# -*- coding: utf-8 -*-
from odoo import models, fields


class CrmLeadLmsWeek(models.Model):
    _name = "crm.lead.lms.week"
    _description = "LMS Training Week (KT / Skill Upload Tracking)"
    _order = "lead_id, week_number"

    # LMS Pipeline sheet, Stage 7 ("the hardest stage in the project"): the number of
    # training weeks varies per lead, so this is a real child model (one row per week)
    # generated from the lead's own Training Duration date range, instead of a fixed
    # set of checkbox fields that would break on anything other than exactly 5 weeks -
    # see Build Notes #5 and crm_lead.py's _mz_sync_lms_weeks.
    lead_id = fields.Many2one("crm.lead", string="Lead", required=True, ondelete="cascade", index=True)
    week_number = fields.Integer(string="Week #", required=True)
    kt_uploaded = fields.Boolean(string="KT Uploaded")
    skill_uploaded = fields.Boolean(string="Skill Uploaded")

    _sql_constraints = [
        ("lead_week_uniq", "unique(lead_id, week_number)", "Each training week can only appear once per lead."),
    ]
