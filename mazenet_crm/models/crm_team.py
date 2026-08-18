# -*- coding: utf-8 -*-
from odoo import models, fields

class CrmTeam(models.Model):
    _inherit = "crm.team"

    # Additional metadata or helpers if needed for Mazenet CRM
    x_bu_category = fields.Selection([
        ('corp', 'Corporate'),
        ('tally', 'Tally'),
        ('dmt', 'DMT'),
        ('tech', 'Technology'),
        ('swdev', 'Software Dev'),
        ('mis', 'MIS'),
    ], string="BU Category", default='corp')

    create_lead_id = fields.Many2many('res.users',
        'mazenet_crm_team_create_lead_users_rel', 'team_id', 'user_id',
        string='Create To', domain="[('id', 'in', member_ids)]")


    
