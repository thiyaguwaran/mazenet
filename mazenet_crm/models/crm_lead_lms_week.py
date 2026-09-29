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
    # Client correction, 2026-09-29: 4 checkboxes per week, not 2 - these two original
    # ones are the SCOPE for the week ("was KT/Skill content included in this week's
    # plan at all"), relabelled "Included" now that "Uploaded" means something more
    # specific below. Internal field names unchanged (only the label moved) so no
    # column rename/data migration is needed.
    kt_uploaded = fields.Boolean(string="KT Included")
    skill_uploaded = fields.Boolean(string="Skill Included")
    # The actual upload-done confirmation for the week - separate from "Included"
    # above (a week can be included in scope well before its content is actually
    # uploaded). Originally built 2026-09-29 as two lead-level checkboxes on Stage 8 -
    # Project State; corrected same day to live here instead, as per-week columns
    # alongside the two above - the label ("... for all weeks") is unchanged from
    # that original spec, just relocated: it reads as "was this week's content
    # actually uploaded, as opposed to only planned/included" for each row.
    kt_content_uploaded = fields.Boolean(string="KT Uploaded for all weeks")
    skill_content_uploaded = fields.Boolean(string="Skill Uploaded for all weeks")

    _sql_constraints = [
        ("lead_week_uniq", "unique(lead_id, week_number)", "Each training week can only appear once per lead."),
    ]
