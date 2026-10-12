# -*- coding: utf-8 -*-
import json
import time
import requests
from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from .api_log import mask_headers

class ApiConfiguration(models.Model):
    _name = 'api.configuration'
    _description = 'API Configuration'
    _order = 'name'

    name = fields.Char(string='Name', required=True)
    server_id = fields.Many2one("api.server.details", required=True, ondelete="cascade")
    route = fields.Char(
        string='Route', required=True,
        help="Incoming: the route this server exposes (e.g. /api/v1/leads). "
             "Outgoing: the route / URL of the other system that is called.")
    direction = fields.Selection(
        selection=[('incoming', 'Incoming'), ('outgoing', 'Outgoing')],
        string='Direction', required=True, default='incoming',
        help="Incoming: other systems call us. Outgoing: we call another system.")
    http_method = fields.Selection(
        selection=[
            ('get', 'GET'),
            ('post', 'POST'),
            ('put', 'PUT'),
            ('patch', 'PATCH'),
            ('delete', 'DELETE'),
        ],
        string='HTTP Method', required=True, default='post')
    operation_type = fields.Selection(
        selection=[
            ('create', 'Create'),
            ('read', 'Read'),
            ('update', 'Update'),
            ('delete', 'Delete'),
        ],
        string='Operation Type', required=True, default='create',
        help="What the API does with the data.")

    save_in = fields.Selection(
        [('none', 'Only keep it in the API log'), ('skill_type', 'Skill Type (create / update)')],
        string='Save the answer in', default='none', required=True,
        help="Skill Type: every item of the answer is created, or updated when its bu_id already exists. "
             "The answer must contain name, short_name, team and bu_id for each item.")

    payload = fields.Text(
        help="JSON body for POST, or query params for GET")

    active = fields.Boolean(string='Active', default=True)

    # result of the last sync
    state = fields.Selection(
        [('draft', 'Not synced'), ('success', 'Success'), ('failed', 'Failed')],
        string='Last Sync', default='draft', readonly=True, copy=False)
    last_sync = fields.Datetime(string='Last Sync Date', readonly=True, copy=False)

    log_ids = fields.One2many('api.log', 'configuration_id', string='Logs')
    log_count = fields.Integer(compute='_compute_log_count')

    def _compute_log_count(self):
        counts = dict(self.env['api.log']._read_group(
            [('configuration_id', 'in', self.ids)], ['configuration_id'], ['__count']))
        for rec in self:
            rec.log_count = counts.get(rec, 0)

    def action_view_logs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('API Logs'),
            'res_model': 'api.log',
            'view_mode': 'list,form',
            'domain': [('configuration_id', '=', self.id)],
        }

    @api.constrains('payload')
    def _check_payload(self):
        for rec in self:
            try:
                rec._get_payload()
            except ValueError as e:
                raise ValidationError(_("The payload is not valid JSON: %s", e))

    def _get_payload(self):
        return json.loads(self.payload) if (self.payload or '').strip() else {}

    # url build ###################################
    def _build_url(self):
        self.ensure_one()
        if self.route.startswith('http'):
            return self.route # the route is already a full url
        if self.server_id.server_type not in ('rest_api', 'soap'):
            raise UserError(_("Only REST API and SOAP servers can be called."))
        return f"{self.server_id.base_url.rstrip('/')}/{self.route.lstrip('/')}"

    # sync functionality ###########################
    def sync_data(self):
        logs = self.env['api.log']
        for rec in self:
            logs |= rec._call_api()
        failed = logs.filtered(lambda log: log.status == 'failed')
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync failed") if failed else _("Sync"),
                "message": failed[:1].error_message or _("Sync finished."),
                "type": "danger" if failed else "success",
                "sticky": bool(failed),
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def _call_api(self):
        """ Call the API once and save the call (request and response) in the API log. """
        self.ensure_one()
        if self.direction != 'outgoing':
            raise UserError(_("Only outgoing APIs can be synced."))
        server = self.server_id.sudo()
        method = self.http_method.upper()
        url = self._build_url()
        headers = server._get_headers()
        data = self._get_payload() or None

        log = {'direction': 'outgoing', 'http_method': method, 'url': url, 'status': 'failed',
               'request_headers': mask_headers(headers, server.api_key_header), 'request_body': data}
        start = time.monotonic()
        try:
            response = requests.request(
                method, url, headers=headers, auth=server._get_auth(), timeout=server.timeout or 30,
                verify=server.verify_ssl,
                params=data if method == 'GET' else None,       # GET: payload in the url
                json=None if method == 'GET' else data,         # others: payload in the body
                allow_redirects=False)                          # a redirect usually means a wrong url / port
            log.update(response_code=response.status_code, response_body=response.text)
            if response.is_redirect:
                raise ValueError(_("The server redirected the call (HTTP %s) to %s. "
                                   "Check the host, the port and the route.",
                                   response.status_code, response.headers.get('Location')))
            response.raise_for_status()                         # HTTP 4xx / 5xx -> error
            if self.save_in == 'skill_type':
                try:
                    answer = response.json()
                except ValueError:
                    raise ValueError(_("The answer is not JSON, it cannot be saved in Skill Type."))
                with self.env.cr.savepoint():                   # all or nothing
                    self._save_skill_types(answer)
            log['status'] = 'success'
        except Exception as e:                                  # connection error, HTTP error, redirect, bad answer
            log['error_message'] = str(e)
            if isinstance(e, requests.exceptions.SSLError) and server.use_ssl:
                log['error_message'] += _("\n\nHint: the server may not use HTTPS. "
                                          "Untick 'Use HTTPS / SSL' on the server and try again.")

        log = self.env['api.log'].create(dict(
            log, duration=time.monotonic() - start, configuration_id=self.id,
            server_id=server.id, company_id=server.company_id.id))
        self.write({'state': log.status, 'last_sync': fields.Datetime.now()})
        return log

    def _save_skill_types(self, answer):
        """ Create the skill types of the answer, or update them when the bu_id already exists. """
        items = answer.get('data') if isinstance(answer, dict) else answer
        if not isinstance(items, list):
            raise UserError(_('The answer must be a list (or {"data": [...]}).'))
        for item in items:
            missing = [key for key in ('bu_name','shortname','id') if item.get(key) in (None, '')]
            if missing:
                raise UserError(_("%(fields)s missing in the answer for: %(item)s",
                                  fields=', '.join(missing), item=json.dumps(item, ensure_ascii=False)))
            # team = self.env['crm.team'].search([('name', '=', item.get('team'))], limit=1)
            # if not team:
            #     raise UserError(_("The team '%s' does not exist.", item.get('team')))
            vals = {'name': item.get('bu_name'), 'short_name': item.get('shortname'),
                    'team_id': False, 'bu_id': int(item.get('id'))}
            skill_type = self.env['skill.type'].search([('bu_id', '=', vals['bu_id'])], limit=1)
            if skill_type:
                skill_type.write(vals)
            else:
                self.env['skill.type'].create(vals)
