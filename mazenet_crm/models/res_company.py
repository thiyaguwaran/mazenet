# -*- coding: utf-8 -*-
from odoo import models, fields, api

class ResCompanyInherit(models.Model):
    _inherit = "res.company"

    grace_time = fields.Integer(
        string="Grace Time", default=20,
        help="RED lock grace period, in minutes: a lead's next activity must be this far "
             "in the past before the lead is RED-locked (20 = a 10:00 activity locks at "
             "10:20). Client spec is 20 minutes - set this to 20."
    )
