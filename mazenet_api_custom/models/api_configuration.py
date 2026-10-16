# -*- coding: utf-8 -*-
import hmac
import json
import secrets
import time
from datetime import date
import requests
from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from .api_log import mask_headers

class ApiConfiguration(models.Model):
    _name = 'api.configuration'
    _inherit = ['mail.thread']
    _description = 'API Configuration'
    _order = 'name'

    name = fields.Char(tracking=True, string='Name', required=True)
    server_id = fields.Many2one(
        "api.server.details", tracking=True, ondelete="cascade",
        help="The server that is called. Not used by incoming APIs (Fetch Dates).")
    route = fields.Char(tracking=True, 
        string='Route', required=True,
        help="The route / URL of the other system that is called.")

    api_type = fields.Selection(tracking=True, 
        selection=[('buisiness_units', 'Buisiness Units'), 
        ('buisiness_services', 'Buisiness Services'),('create_work_order', 'Create work Order'),
        ('fetch_dates', 'Fetch Dates (incoming)')],
        string='API Identification', required=True,
        help="This is to know which API call")
    http_method = fields.Selection(tracking=True, 
        selection=[
            ('get', 'GET'),
            ('post', 'POST'),
            ('put', 'PUT'),
            ('patch', 'PATCH'),
            ('delete', 'DELETE'),
        ],
        string='HTTP Method', required=True, default='post')
    operation_type = fields.Selection(tracking=True, 
        selection=[
            ('create', 'Create'),
            ('read', 'Read'),
            ('update', 'Update'),
            ('delete', 'Delete'),
        ],
        string='Operation Type', required=True, default='create',
        help="What the API does with the data.")

    save_in = fields.Selection(
        [('none', 'Only keep it in the API log'), 
        ('skill_type', 'Skill Type (create / update)')],
        tracking=True, string='Save the answer in', default='none', required=True,
        help="Skill Type: every item of the answer is created, or updated when its bu_id already exists. "
             "The answer must contain name, short_name, team and bu_id for each item.")

    incoming_token = fields.Char(
        string='Token', copy=False,
        help="Bearer token the other system must send to call this incoming API (Fetch Dates).")
    incoming_url = fields.Char(string='API URL', compute='_compute_incoming_url')

    payload = fields.Text(tracking=True, 
        help="JSON body for POST, or query params for GET")

    active = fields.Boolean(tracking=True, string='Active', default=True)

    # result of the last sync
    state = fields.Selection(
        [('draft', 'Not synced'), ('success', 'Success'), ('failed', 'Failed')],
        string='Last Sync', default='draft', readonly=True, copy=False)
    last_sync = fields.Datetime(string='Last Sync Date', readonly=True, copy=False)

    log_ids = fields.One2many('api.log', 'configuration_id', string='Logs')
    log_count = fields.Integer(compute='_compute_log_count')

    def _compute_incoming_url(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        for rec in self:
            rec.incoming_url = ('%s/api/mazenet/%s' % (base_url, rec.route.strip('/'))
                                if rec.api_type == 'fetch_dates' and rec.route else False)

    def action_generate_token(self):
        for rec in self:
            rec.incoming_token = secrets.token_urlsafe(32)

    @api.constrains('api_type', 'server_id', 'incoming_token')
    def _check_server_or_token(self):
        for rec in self:
            if rec.api_type == 'fetch_dates':
                if not rec.incoming_token:
                    raise ValidationError(_("Generate a token for the incoming API."))
            elif not rec.server_id:
                raise ValidationError(_("Select the server."))

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
            if rec.api_type == 'create_work_order':
                raise UserError(_("Work orders are created from the lead, this API cannot be synced."))
            if rec.api_type == 'fetch_dates':
                raise UserError(_("This is an incoming API, the other system calls it. It cannot be synced."))
            if rec.api_type == 'buisiness_services':
                # one call per skill type, with its bu_id in the url
                logs |= rec._sync_business_services(self.env['skill.type'].search([('bu_id', '!=', 0)]))
            else:
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

    def _call_api(self, extra_params=None, skill_type=None, path_param=None, payload=None, on_success=None):
        """ Call the API once and save the call (request and response) in the API log.

        :param extra_params: dict merged into the payload / query params (e.g. {'bu_id': 2}).
        :param path_param: value added at the end of the route (or put in its {bu_id} placeholder).
        :param payload: dict sent instead of the configured payload.
        :param on_success: callable(answer) run with the JSON answer; an answer with
                           ``success: false`` is treated as an error.
        :param skill_type: skill.type record whose business services are saved from the answer
                           (only for the 'Buisiness Services' API).
        """
        self.ensure_one()
        server = self.server_id.sudo()
        method = self.http_method.upper()
        url = self._build_url()
        if path_param is not None:                              # /api/crm/services/ -> /api/crm/services/2
            url = (url.replace('{bu_id}', str(path_param)) if '{bu_id}' in url
                   else '%s/%s' % (url.rstrip('/'), path_param))
        headers = server._get_headers()
        data = (payload if payload is not None
                else dict(self._get_payload() or {}, **(extra_params or {}))) or None

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
            if not response.ok:                                 # HTTP 4xx / 5xx -> error, with the API's own message
                try:
                    api_message = response.json().get('message')
                except (ValueError, AttributeError):
                    api_message = None
                if api_message:
                    raise ValueError("HTTP %s: %s" % (response.status_code, api_message))
            response.raise_for_status()
            if self.save_in == 'skill_type' and self.api_type != 'buisiness_services':
                try:
                    answer = response.json()
                except ValueError:
                    raise ValueError(_("The answer is not JSON, it cannot be saved in Skill Type."))
                with self.env.cr.savepoint():                   # all or nothing
                    self._save_skill_types(answer)
            if skill_type and self.api_type == 'buisiness_services':
                try:
                    answer = response.json()
                except ValueError:
                    raise ValueError(_("The answer is not JSON, it cannot be saved in Business Services."))
                with self.env.cr.savepoint():
                    self._save_business_services(answer, skill_type)
            if on_success:
                try:
                    answer = response.json()
                except ValueError:
                    raise ValueError(_("The answer is not JSON."))
                if isinstance(answer, dict) and answer.get('success') is False:
                    raise ValueError(answer.get('message') or _("The API returned an error."))
                with self.env.cr.savepoint():
                    on_success(answer)
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

    def _save_business_services(self, answer, skill_type):
        """ Create the business services of the answer under the skill type,
        or update them when the same maze_id already exists for it. """
        items = answer.get('data') if isinstance(answer, dict) else answer
        if not isinstance(items, list):
            raise UserError(_('The answer must be a list (or {"data": [...]}).'))
        BusinessService = self.env['business.service']
        for item in items:
            missing = [key for key in ('id', 'services') if item.get(key) in (None, '')]
            if missing:
                raise UserError(_("%(fields)s missing in the answer for: %(item)s",
                                  fields=', '.join(missing), item=json.dumps(item, ensure_ascii=False)))
            vals = {'maze_id': int(item['id']), 'services': item['services'],
                    'shortname': item.get('shortname') or False}
            service = BusinessService.search([
                ('skill_type_id', '=', skill_type.id), ('maze_id', '=', vals['maze_id'])], limit=1)
            if service:
                service.write(vals)
            else:
                BusinessService.create(dict(vals, skill_type_id=skill_type.id))

    def _sync_business_services(self, skill_types):
        """ Call this API once per skill type (bu_id in the url) and return the logs. """
        logs = self.env['api.log']
        for skill_type in skill_types:
            logs |= self._call_api(path_param=skill_type.bu_id, skill_type=skill_type)
        return logs

    @api.model
    def _fetch_business_services(self, skill_types):
        """ Fetch the business services of new skill types with the configured 'Buisiness Services' API. """
        config = self.sudo().search([('api_type', '=', 'buisiness_services')], limit=1)
        if config:
            config._sync_business_services(skill_types.filtered('bu_id'))

    # incoming APIs (the other system calls Odoo) ###########################
    @api.model
    def _handle_incoming(self, route, headers, body, url=None):
        """ Run the incoming API configured with this route and save the call in the API log.

        :param route: route called, without /api/mazenet/.
        :param headers: request headers (dict).
        :param body: raw request body.
        :param url: full url called (for the log).
        :return: (response dict, http status)
        """
        start = time.monotonic()
        configs = self.sudo().search([('api_type', '=', 'fetch_dates')])
        config = configs.filtered(lambda c: (c.route or '').strip('/') == route.strip('/'))[:1]
        if not config:
            return {'success': False, 'message': "Unknown API."}, 404
        authorization = headers.get('Authorization', '')
        if not config._check_incoming_token(authorization):
            payload, status = {'success': False, 'message': "Invalid or missing token."}, 401
        else:
            try:
                with self.env.cr.savepoint():
                    payload, status = config._update_work_order_dates(body)
            except Exception as e:      # never lose the log because of an unexpected error
                payload, status = {'success': False, 'message': str(e)}, 500
        # the call is saved whatever the result (the caller gets a status, not an exception)
        self.env['api.log'].sudo().create({
            'configuration_id': config.id,
            'server_id': config.server_id.id,
            'company_id': config.server_id.company_id.id or self.env.company.id,
            'direction': 'incoming',
            'http_method': 'POST',
            'url': url or route,
            'request_headers': mask_headers(dict(headers)),
            'request_body': body,
            'response_code': status,
            'response_body': payload,
            'status': 'success' if status == 200 else 'failed',
            'error_message': payload.get('message') if status != 200 else False,
            'duration': time.monotonic() - start,
        })
        config.write({'state': 'success' if status == 200 else 'failed',
                      'last_sync': fields.Datetime.now()})
        return payload, status

    def _check_incoming_token(self, authorization):
        """ Bearer token of this configuration; no token configured = closed. """
        self.ensure_one()
        expected = self.incoming_token or ''
        given = authorization[7:].strip() if (authorization or '').lower().startswith('bearer ') else ''
        return bool(expected) and hmac.compare_digest(given, expected)

    def _update_work_order_dates(self, body):
        """ Body: {"query_id": 928, "wo_no": 12, "work_start_date": "2026-10-20", "work_end_date": "2026-10-25"}
        Finds the lead(s) created with that query id and work order number and stores the dates. """
        self.ensure_one()
        try:
            body = json.loads(body or '{}')
            query_id = int(body['query_id'])
            wo_no = int(body['wo_no'])
            start = date.fromisoformat(body['work_start_date'])
            end = date.fromisoformat(body['work_end_date'])
        except (ValueError, KeyError, TypeError):
            return {'success': False, 'message':
                    "Body must be JSON with query_id and wo_no (integers) and work_start_date / "
                    "work_end_date (YYYY-MM-DD)."}, 400
        if end < start:
            return {'success': False, 'message': "work_end_date cannot be before work_start_date."}, 400

        leads = self.env['crm.lead'].sudo().with_context(active_test=False).search(
            [('maze_query_id', '=', query_id), ('maze_wo_no', '=', wo_no)])
        if not leads:
            return {'success': False, 'message': "No lead found for query %s / work order %s." % (query_id, wo_no)}, 404
        leads.write({'work_start_date': start, 'work_end_date': end})
        return {
            'success': True, 'message': "Work order dates updated.",
            'data': {'query_id': query_id, 'wo_no': wo_no, 'leads': leads.ids,
                     'work_start_date': start.isoformat(), 'work_end_date': end.isoformat()},
        }, 200
