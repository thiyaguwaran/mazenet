# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

DEFAULT_PORTS = {'sftp': 22, 'ftp': 21, 'soap': 443, 'rest_api': 443}


class ApiServerDetails(models.Model):
    _name = 'api.server.details'
    _description = 'API Server Details'
    _order = 'name'

    # ---- General
    name = fields.Char(string='Server Name', required=True)
    server_type = fields.Selection(
        selection=[
            ('rest_api', 'REST API'),
            ('soap', 'SOAP'),
            ('sftp', 'SFTP'),
            ('ftp', 'FTP'),
            ('other', 'Other'),
        ],
        string='Server Type', required=True, default='rest_api')
    active = fields.Boolean(string='Active', default=True)
    company_id = fields.Many2one(
        'res.company', string='Company', required=True, default=lambda self: self.env.company)

    # ---- Connection
    host = fields.Char(string='Host', required=True, help="Host name or IP address, e.g. api.example.com")
    port = fields.Integer(
        string='Port', compute='_compute_port', store=True, readonly=False,
        help="Filled with the usual port of the server type (SFTP 22, FTP 21, HTTPS 443, HTTP 80); change it if needed.")
    use_ssl = fields.Boolean(string='Use HTTPS / SSL', default=True)
    verify_ssl = fields.Boolean(string='Verify SSL Certificate', default=True)
    timeout = fields.Integer(string='Timeout (seconds)', default=30)
    base_url = fields.Char(string='Base URL', compute='_compute_base_url')

    # ---- Authentication
    auth_type = fields.Selection(
        selection=[
            ('none', 'None'),
            ('basic', 'User Name / Password'),
            ('api_key', 'API Key'),
            ('bearer', 'Bearer Token'),
            ('ssh_key', 'SSH Private Key'),
        ],
        string='Authentication', required=True, default='basic')
    user_name = fields.Char(string='User Name')
    password = fields.Char(string='Password')
    api_key = fields.Char(string='API Key')
    api_key_header = fields.Char(string='API Key Header', default='X-API-Key')
    token = fields.Char(string='Bearer Token')
    private_key = fields.Text(string='SSH Private Key')

    notes = fields.Text(string='Notes')

    _name_company_uniq = models.Constraint(
        'unique(name, company_id)', 'A server with this name already exists for this company.')

    @api.depends('server_type', 'use_ssl')
    def _compute_port(self):
        for rec in self:
            if rec.server_type == 'rest_api':
                rec.port = 443 if rec.use_ssl else 80
            else:
                rec.port = DEFAULT_PORTS.get(rec.server_type, 0)

    @api.depends('server_type', 'host', 'port', 'use_ssl')
    def _compute_base_url(self):
        for rec in self:
            if rec.server_type in ('rest_api', 'soap') and rec.host:
                scheme = 'https' if rec.use_ssl else 'http'
                default = 443 if rec.use_ssl else 80
                port = '' if rec.port in (0, default) else ':%s' % rec.port
                rec.base_url = '%s://%s%s' % (scheme, rec.host.strip().strip('/'), port)
            elif rec.server_type in ('sftp', 'ftp') and rec.host:
                rec.base_url = '%s://%s:%s' % (rec.server_type, rec.host.strip().strip('/'), rec.port)
            else:
                rec.base_url = False

    @api.constrains('port')
    def _check_port(self):
        for rec in self:
            if not 0 <= rec.port <= 65535:
                raise ValidationError(_("The port must be between 0 and 65535."))

    @api.constrains('timeout')
    def _check_timeout(self):
        for rec in self:
            if rec.timeout < 0:
                raise ValidationError(_("The timeout cannot be negative."))
