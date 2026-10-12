# -*- coding: utf-8 -*-
import json

from odoo import api, fields, models

MAX_TEXT = 20000    # longer request / response bodies are cut in the log


def mask_headers(headers, api_key_header=None):
    """ Copy of the headers without secrets, safe to store in the log. """
    secret = {'authorization', 'proxy-authorization', 'cookie', (api_key_header or '').lower()}
    return {k: '********' if k.lower() in secret else v for k, v in headers.items()}


class ApiLog(models.Model):
    """ One row per API call (outgoing calls we make, incoming calls we receive). """
    _name = 'api.log'
    _description = 'API Log'
    _order = 'id desc'

    name = fields.Char(string='Request', readonly=True)
    configuration_id = fields.Many2one('api.configuration', string='API', ondelete='set null', index=True, readonly=True)
    server_id = fields.Many2one('api.server.details', string='Server', ondelete='set null', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', default=lambda self: self.env.company, readonly=True)
    direction = fields.Selection([('incoming', 'Incoming'), ('outgoing', 'Outgoing')], readonly=True)
    http_method = fields.Char(string='Method', readonly=True)
    url = fields.Char(string='URL', readonly=True)
    request_headers = fields.Text(readonly=True, help="Secrets (Authorization, API key) are never stored.")
    request_body = fields.Text(readonly=True)
    response_code = fields.Integer(string='HTTP Code', readonly=True)
    response_body = fields.Text(readonly=True)
    status = fields.Selection([('success', 'Success'), ('failed', 'Failed')], readonly=True, index=True)
    error_message = fields.Text(string='Error', readonly=True)
    duration = fields.Float(string='Duration (s)', digits=(10, 3), readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals.setdefault('name', ('%s %s' % (vals.get('http_method', ''), vals.get('url', '')))[:200])
            for field in ('request_headers', 'request_body', 'response_body'):
                value = vals.get(field)
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, indent=2, ensure_ascii=False)
                vals[field] = (value or '')[:MAX_TEXT]
        return super().create(vals_list)
