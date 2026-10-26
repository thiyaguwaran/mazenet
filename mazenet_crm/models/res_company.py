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
    release_grace_time = fields.Integer(
        string="Grace Time After Release", default=20,
        help="RED lock grace period after a RELEASE, in minutes: once the lock is released the "
             "lead can't be RED-locked again until this long has passed since the release "
             "(20 = released at 10:30, earliest re-lock 10:50). Separate from Grace Time, which "
             "only delays the FIRST lock after an activity's due moment."
    )
