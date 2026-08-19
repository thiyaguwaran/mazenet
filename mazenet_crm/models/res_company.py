# -*- coding: utf-8 -*-
from odoo import models, fields, api

class ResCompanyInherit(models.Model):
    _inherit = "res.company"

    grace_time = fields.Integer(string="Grace Time", 
                default=15, help="Grace time in minutes for lead assignment.")

