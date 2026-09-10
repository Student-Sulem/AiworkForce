"""Stripe: payment processing integration.

WHAT THIS DOES FOR THE WORKFORCE
--------------------------------
Stripe connects the platform to real payment data. The Support employee
checks a customer's invoice or payment history, the Marketing employee
reports on revenue metrics, and the Finance-related tools verify
subscription status -- all without a person logging into the Stripe dashboard.
"""

from .base import CallResult, ConfigField, Connector
from . import register


@register
class StripeConnector(Connector):
    key = 'stripe'
    name = 'Stripe'
    description = 'Payment processing: read invoices, payments, subscriptions and revenue data.'
    category = 'communication'
    icon = 'fa-credit-card'
    color = '#635bff'

    config_fields = (
        ConfigField('secret_key', 'Secret key', secret=True, required=True,
                    help_text='Stripe secret key (sk_live_... or sk_test_...)'),
        ConfigField('mode', 'Mode', field_type='choice', required=False,
                    default='auto',
                    choices=(('auto', 'Automatic — live when key is present, demo otherwise'),
                             ('live', 'Live only'), ('demo', 'Demo only'))),
    )

    operations = (
        'list_charges', 'get_charge', 'list_customers',
        'get_customer', 'list_invoices',
    )

    def live_list_charges(self, limit=10):
        key = self.setting('secret_key')
        status, data = self.request_json(
            'https://api.stripe.com/v1/charges',
            headers={'Authorization': f'Bearer {key}'},
            params={'limit': min(limit, 100)})
        charges = data.get('data', [])
        return self.ok(f'Found {len(charges)} recent charges.',
                       {'charges': [{'id': c['id'], 'amount': c['amount'] / 100,
                                     'currency': c['currency'].upper(),
                                     'status': c['status'],
                                     'customer': c.get('customer', ''),
                                     'date': c.get('created', '')}
                                    for c in charges[:limit]]})

    def demo_list_charges(self, limit=10):
        return self.simulated(f'Retrieved {limit} simulated charges.',
                              {'charges': [
                                  {'id': 'ch_demo_1', 'amount': 49.99, 'currency': 'USD',
                                   'status': 'succeeded', 'customer': 'cus_demo_1',
                                   'date': '2026-09-01'},
                                  {'id': 'ch_demo_2', 'amount': 199.99, 'currency': 'USD',
                                   'status': 'succeeded', 'customer': 'cus_demo_2',
                                   'date': '2026-09-02'},
                              ]})

    def live_get_charge(self, charge_id):
        key = self.setting('secret_key')
        status, data = self.request_json(
            f'https://api.stripe.com/v1/charges/{charge_id}',
            headers={'Authorization': f'Bearer {key}'})
        return self.ok(f'Retrieved charge {charge_id}: ${data.get("amount", 0) / 100:.2f}',
                       {'id': data['id'], 'amount': data['amount'] / 100,
                        'currency': data['currency'].upper(),
                        'status': data['status'],
                        'customer': data.get('customer', ''),
                        'receipt_email': data.get('receipt_email', ''),
                        'date': data.get('created', '')})

    def demo_get_charge(self, charge_id):
        return self.simulated(f'Retrieved simulated charge {charge_id}.',
                              {'id': charge_id, 'amount': 49.99, 'currency': 'USD',
                               'status': 'succeeded'})

    def live_list_customers(self, limit=10):
        key = self.setting('secret_key')
        status, data = self.request_json(
            'https://api.stripe.com/v1/customers',
            headers={'Authorization': f'Bearer {key}'},
            params={'limit': min(limit, 100)})
        customers = data.get('data', [])
        return self.ok(f'Found {len(customers)} customers.',
                       {'customers': [{'id': c['id'], 'email': c.get('email', ''),
                                       'name': c.get('name', ''),
                                       'created': c.get('created', '')}
                                      for c in customers[:limit]]})

    def demo_list_customers(self, limit=10):
        return self.simulated(f'Retrieved {limit} simulated customers.',
                              {'customers': [
                                  {'id': 'cus_demo_1', 'email': 'customer@example.com',
                                   'name': 'Acme Corp', 'created': '2026-01-01'},
                              ]})

    def live_list_invoices(self, customer_id=None, limit=10):
        key = self.setting('secret_key')
        params = {'limit': min(limit, 100)}
        if customer_id:
            params['customer'] = customer_id
        status, data = self.request_json(
            'https://api.stripe.com/v1/invoices',
            headers={'Authorization': f'Bearer {key}'},
            params=params)
        invoices = data.get('data', [])
        return self.ok(f'Found {len(invoices)} invoices.',
                       {'invoices': [{'id': inv['id'], 'amount': inv['amount_due'] / 100,
                                      'currency': inv['currency'].upper(),
                                      'status': inv['status'],
                                      'customer': inv.get('customer', ''),
                                      'date': inv.get('created', '')}
                                     for inv in invoices[:limit]]})

    def demo_list_invoices(self, customer_id=None, limit=10):
        return self.simulated(f'Retrieved {limit} simulated invoices.',
                              {'invoices': [
                                  {'id': 'inv_demo_1', 'amount': 499.99, 'currency': 'USD',
                                   'status': 'paid', 'customer': 'cus_demo_1', 'date': '2026-09-01'},
                              ]})