# -*- coding: utf-8 -*-
from odoo import fields, models


class ApiConfiguration(models.Model):
    _name = 'api.configuration'
    _description = 'API Configuration'
    _order = 'name'

    name = fields.Char(string='Name', required=True)
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
    active = fields.Boolean(string='Active', default=True)
