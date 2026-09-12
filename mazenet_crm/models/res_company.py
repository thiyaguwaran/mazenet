# -*- coding: utf-8 -*-
from odoo import models, fields, api

class ResCompanyInherit(models.Model):
    _inherit = "res.company"

    grace_time = fields.Integer(
        string="Grace Time", default=15,
        help="No longer used by the RED lock (crm_lead.py's MZ_ACTIVITY_WINDOW_MINUTES is "
             "now a fixed 20 minutes, per the client's rework spec) - kept only so this "
             "setting doesn't silently vanish for whoever configured it."
    )

