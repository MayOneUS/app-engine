"""Handlers for MayOne.US."""

from collections import namedtuple, defaultdict
import datetime
import json
import logging
import cgi

from google.appengine.api import mail
from google.appengine.ext import db
from google.appengine.ext import deferred
import validictory
import webapp2

import cache
import model
import templates
import util


# Immutable environment with both configuration variables, and backends to be
# mocked out in tests.
Environment = namedtuple(
  'Environment',
  [
    # App engine app name, or 'local' for dev_appserver, or 'unittest' for unit
    # tests.
    'app_name',

    'stripe_public_key',

    # StripeBackend
    'stripe_backend',

    # MailingListSubscriber
    'mailing_list_subscriber',

    # MailSender
    'mail_sender',
  ])


class PaymentError(Exception):
  pass


class StripeBackend(object):
  """Interface which contacts stripe."""

  def CreateCustomer(self, email, card_token):
    """Creates a stripe customer so we can charge them later.

    Returns: A string customer id.
    """
    raise NotImplementedError()

  def Charge(self, customer_id, amount_cents):
    """Charges a customer and returns an identifier for the charge."""
    raise NotImplementedError()


class MailingListSubscriber(object):
  """Interface which signs folks up for emails."""
  def Subscribe(self, email, first_name, last_name, amount_cents, ip_addr, time,
                source, phone=None, zipcode=None, volunteer=None, skills=None,
                rootstrikers=None, nonce=None, pledgePageSlug=None):
    raise NotImplementedError()


_STR = dict(type='string')
class PledgeHandler(webapp2.RequestHandler):
  """RESTful handler for pledge objects."""

  CREATE_SCHEMA = dict(
    type='object',
    properties=dict(
      email=_STR,
      phone=dict(type='string', blank=True),
      name=_STR,
      occupation=_STR,
      employer=_STR,
      target=_STR,
      subscribe=dict(type='boolean'),
      anonymous=dict(type='boolean'),
      amountCents=dict(type='integer', minimum=100),
      pledgeType=dict(enum=model.Pledge.TYPE_VALUES, required=False),
      team=dict(type='string', blank=True),

      payment=dict(type='object',
                   properties=dict(
                     STRIPE=dict(type='object',
                                 required=False,
                                 properties=dict(token=_STR)),
                     # TODO: Paypal
                   )
                 ),
    )
  )

  def post(self):
    """Create a new pledge, and update user info."""
    util.EnableCors(self)
    self.response.headers['Content-Type'] = 'application/json'
    env = self.app.config['env']

    try:
      data = json.loads(self.request.body)
    except ValueError, e:
      logging.warning('Bad JSON request: %s', e)
      self.error(400)
      self.response.write('Invalid request')
      return

    try:
      validictory.validate(data, PledgeHandler.CREATE_SCHEMA)
    except ValueError, e:
      logging.warning('Schema check failed: %s', e)
      self.error(400)
      self.response.write('Invalid request')
      return

    # Do any server-side processing the payment processor needs.
    stripe_customer_id = None
    stripe_charge_id = None
    if 'STRIPE' in data['payment']:
      stripe_customer_id = env.stripe_backend.CreateCustomer(
        email=data['email'], card_token=data['payment']['STRIPE']['token'])
      try:
        stripe_charge_id = env.stripe_backend.Charge(stripe_customer_id,
                                                     data['amountCents'])
      except PaymentError, e:
        logging.warning('Payment error: %s', e)
        self.error(400)
        json.dump(dict(paymentError=str(e)), self.response)
        return
    else:
      logging.warning('No payment processor specified: %s', data)
      self.error(400)
      return

    # Split apart the name into first and last. Yes, this sucks, but adding the
    # name fields makes the form look way more daunting. We may reconsider this.
    name_parts = data['name'].split(None, 1)
    first_name = name_parts[0]
    if len(name_parts) == 1:
      last_name = ''
      logging.warning('Could not determine last name: %s', data['name'])
    else:
      last_name = name_parts[1]

    user, pledge = model.addPledge(email=data['email'],
                             stripe_customer_id=stripe_customer_id,
                             stripe_charge_id=stripe_charge_id,
                             amount_cents=data['amountCents'],
                             first_name=first_name,
                             last_name=last_name,
                             occupation=data['occupation'],
                             employer=data['employer'],
                             phone=data['phone'],
                             target=data['target'],
                             pledge_type=data.get(
                               'pledgeType', model.Pledge.TYPE_CONDITIONAL),
                             team=data['team'],
                             mail_list_optin=data['subscribe'],
                             anonymous=data['anonymous'])

    if data['subscribe']:
      env.mailing_list_subscriber.Subscribe(
        email=data['email'],
        first_name=first_name, last_name=last_name,
        amount_cents=data['amountCents'],
        ip_addr=self.request.remote_addr,
        time=datetime.datetime.now(),
        source='pledge',
        phone=data['phone'],
        nonce=user.url_nonce)

    # Add to the total.
    model.ShardedCounter.increment('TOTAL-5', data['amountCents'])

    if data['team']:
      cache.IncrementTeamPledgeCount(data['team'], 1)
      cache.IncrementTeamTotal(data['team'], data['amountCents'])

    format_kwargs = {
      'name': data['name'].encode('utf-8'),
      'url_nonce': pledge.url_nonce,
      'total': '$%d' % int(data['amountCents'] / 100),
      'user_url_nonce': user.url_nonce
    }

    text_body = open('email/thank-you.txt').read().format(**format_kwargs)
    html_body = open('email/thank-you.html').read().format(**format_kwargs)

    env.mail_sender.Send(to=data['email'].encode('utf-8'),
                         subject='Thank you for your pledge',
                         text_body=text_body,
                         html_body=html_body)

    id = str(pledge.key())
    receipt_url = '/receipt/%s?auth_token=%s' % (id, pledge.url_nonce)

    json.dump(dict(id=id,
                   auth_token=pledge.url_nonce,
                   receipt_url=receipt_url), self.response)

  def options(self):
    util.EnableCors(self)

class SubscribeHandler(webapp2.RequestHandler):
  """RESTful handler for subscription requests."""
  # https://www.pivotaltracker.com/s/projects/1075614/stories/71725060

  def post(self):
    env = self.app.config['env']

    logging.info('body: %s' % self.request.body)

    email_input = cgi.escape(self.request.get('email'))
    if len(email_input) == 0:
      logging.warning("Bad Request: required field (email) missing.")
      self.error(400)
      return

    first_name = cgi.escape(self.request.get('first_name'))
    if len(first_name) == 0:
      first_name = None

    last_name = cgi.escape(self.request.get('last_name'))
    if len(last_name) == 0:
      last_name = None

    phone_input = cgi.escape(self.request.get('phone'))
    if len(phone_input) == 0:
      phone_input = None

    zipcode_input = cgi.escape(self.request.get('zipcode'))
    if len(zipcode_input) == 0:
      zipcode_input = None

    phone_input = cgi.escape(self.request.get('phone'))
    if len(phone_input) == 0:
      phone_input = None

    volunteer_input = cgi.escape(self.request.get('volunteer')) # "YES" or "NO"
    if volunteer_input == 'on':
      volunteer_input = 'Yes'
    elif volunteer_input == 'off':
      volunteer_input = ''

    skills_input = cgi.escape(self.request.get('skills')) #Free text, limited to 255 char
    if len(skills_input) == 0:
      skills_input = None

    rootstrikers_input = cgi.escape(self.request.get('rootstrikers')) #Free text, limited to 255 char
    if rootstrikers_input=='on':
      rootstrikers_input = 'Yes'
    elif rootstrikers_input=='off':
      rootstrikers_input = ''

    source_input = cgi.escape(self.request.get('source'))
    if len(source_input) == 0:
      source_input = 'subscribe'

    pledgePageSlug_input = cgi.escape(self.request.get('pledgePageSlug'))
    if len(pledgePageSlug_input) == 0:
      pledgePageSlug_input = ''

    env.mailing_list_subscriber.Subscribe(
      email=email_input,
      first_name=first_name, last_name=last_name,
      amount_cents=None,
      ip_addr=self.request.remote_addr,
      time=datetime.datetime.now(),
      source=source_input,
      phone=phone_input,
      zipcode=zipcode_input,
      volunteer=volunteer_input,
      skills=skills_input,
      rootstrikers=rootstrikers_input,
      pledgePageSlug=pledgePageSlug_input
      )

    util.EnableCors(self)
    redirect_input = cgi.escape(self.request.get('redirect'))
    if len(redirect_input)>0:
      redirect_url = '%s?email=%s&source=%s' % (redirect_input, email_input, source_input)
    else:
      redirect_url = '/pledge?email=%s' % email_input
    self.redirect(str(redirect_url))

class ReceiptHandler(webapp2.RequestHandler):
  def get(self, id):
    try:
      pledge = db.get(db.Key(id))
    except db.BadKeyError, e:
      logging.warning('Bad key error: %s', e)
      self.error(404)
      self.response.write('Not found')
      return

    if not pledge:
      self.error(404)
      self.response.write('Not found')
      return

    user = model.User.get_by_key_name(pledge.email)
    if user is None:
      logging.warning('pledge had missing user: %r, %r', id, pledge.email)
      self.error(404)
      self.response.write('Not found')

    # allow this one pledge so test receipt can be viewed
    if (id != 'agxzfm1heWRheS1wYWNyEwsSBlBsZWRnZRiAgICAlZG2CAw'):
      auth_token = self.request.get('auth_token')
      if not util.ConstantTimeIsEqual(auth_token, pledge.url_nonce):
        self.error(403)
        self.response.write('Access denied')
        return

    template = templates.GetTemplate('receipt.html')
    self.response.write(template.render(dict(pledge=pledge, user=user)))


class PaymentConfigHandler(webapp2.RequestHandler):
  def get(self):
    util.EnableCors(self)
    env = self.app.config['env']
    if not env.stripe_public_key:
      raise Error('No stripe public key in DB')
    params = dict(testMode=(env.app_name == u'local'),
                  stripePublicKey=env.stripe_public_key)

    self.response.headers['Content-Type'] = 'application/json'
    json.dump(params, self.response)


class TotalHandler(webapp2.RequestHandler):
  # These get added to every pledge calculation
  STRETCH_GOAL_MATCH = 10000000
  PRE_SHARDING_TOTAL = 59767534  # See model.ShardedCounter
  WP_PLEDGE_TOTAL = 41326868
  DEMOCRACY_DOT_COM_BALANCE = 9951173
  CHECKS_BALANCE = 9065700  # lol US government humor

  def get(self):
    util.EnableCors(self)
    total = (TotalHandler.PRE_SHARDING_TOTAL +
             TotalHandler.WP_PLEDGE_TOTAL +
             TotalHandler.DEMOCRACY_DOT_COM_BALANCE +
             TotalHandler.CHECKS_BALANCE +
             TotalHandler.STRETCH_GOAL_MATCH)
    total += model.ShardedCounter.get_count('TOTAL-5')

    result = dict(totalCents=total)

    team = self.request.get("team")
    if team:
      team_pledges = cache.GetTeamPledgeCount(team) or 0
      team_total = cache.GetTeamTotal(team) or 0
      try:
        # there are some memcache values with string values
        team_total = int(team_total)
      except ValueError, e:
        logging.exception("non-integral team total: %r", team_total)
        team_total = 0

      if not (team_pledges and team_total):
        for pledge in model.Pledge.all().filter("team =", team):
          team_pledges += 1
          team_total += pledge.amountCents
        cache.SetTeamPledgeCount(team, team_pledges)
        cache.SetTeamTotal(team, team_total)

      result['team'] = team
      result['teamPledges'] = team_pledges
      result['teamTotalCents'] = team_total

    self.response.headers['Content-Type'] = 'application/json'
    json.dump(result, self.response)

  options = util.EnableCors


class ThankTeamHandler(webapp2.RequestHandler):
  def post(self):
    env = self.app.config['env']
    util.EnableCors(self)

    for field in ['team', 'reply_to', 'subject', 'message_body', 'new_members']:
      if not field in self.request.POST:
        msg = "Bad Request: required field %s missing." % field
        logging.warning(msg)
        self.error(400)
        self.response.write(msg)
        return self.response

    # get the pldedges for this team, excluding the reply_to
    pledges = model.Pledge.all().filter(
      'team =',self.request.POST['team']).filter(
      'email !=', self.request.POST['reply_to'])

    # if only sending to new members, filter out those that have already received emails

    if self.request.POST['new_members'] == 'True':
      pledges = pledges.filter('thank_you_sent_at =', None)

    i = 0
    for pledge in pledges:
      env.mail_sender.Send(to=pledge.email,
                     subject=self.request.POST['subject'],
                     text_body=self.request.POST['message_body'],
                     html_body=self.request.POST['message_body'],
                     reply_to=self.request.POST['reply_to'])
      i += 1
      # set the thank_you_sent_at for users after sending
      # FIXME: make sure the send was successful
      pledge.thank_you_sent_at = datetime.datetime.now()
      pledge.put()

    logging.info('THANKING: %d PLEDGERS!!' % i)
    self.response.write(i)

  options = util.EnableCors


class PledgersHandler(webapp2.RequestHandler):

  def get(self):
    util.EnableCors(self)

    team = self.request.get("team")
    if not team:
      self.error(400)
      self.response.write('team required')
      return

    pledgers = defaultdict(lambda: 0)

    for pledge in model.Pledge.all().filter("team =", team):
      if pledge.anonymous:
        pledgers["Anonymous"] += pledge.amountCents
        continue
      user = model.User.get_by_key_name(pledge.email)
      if user is None or (not user.first_name and not user.last_name):
        pledgers["Anonymous"] += pledge.amountCents
        continue
      name = ("%s %s" % (user.first_name or "", user.last_name or "")).strip()
      pledgers[name] += pledge.amountCents

    pledgers_by_amount = []
    for name, amount in pledgers.iteritems():
      pledgers_by_amount.append((amount, name))
    pledgers_by_amount.sort(reverse=True)

    result = {"pledgers": [name for _, name in pledgers_by_amount]}

    self.response.headers['Content-Type'] = 'application/json'
    json.dump(result, self.response)

  options = util.EnableCors


HANDLERS = [
  ('/r/pledgers', PledgersHandler),
  ('/r/pledge', PledgeHandler),
  ('/receipt/(.+)', ReceiptHandler),
  ('/r/payment_config', PaymentConfigHandler),
  ('/r/total', TotalHandler),
  ('/r/thank', ThankTeamHandler),
  ('/r/subscribe', SubscribeHandler),
]
