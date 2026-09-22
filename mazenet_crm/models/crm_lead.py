# -*- coding: utf-8 -*-
import re
from datetime import timedelta

import pytz

from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError

MZ_PHONE_RE = re.compile(r'^\d{10}$')
MZ_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

# Build-notes format validation ("Format validation only: valid 10-digit number" /
# "valid email syntax... No dummy-number/dummy-email detection") is the same rule
# repeated verbatim across all 5 M2 pipeline sheets - scoped to just those BUs so
# other teams (Corporate/LMS/TNH/Hunter...) aren't newly constrained by this build.
MZ_FORMAT_VALIDATED_BU_CATEGORIES = {'dmt', 'tally', 'tech', 'swdev', 'mis'}

# The RED lock's own grace period - an activity's real due moment (x_next_activity_datetime)
# has to be this many minutes in the past before x_is_locked actually flips (a 10:00 AM
# activity locks at 10:20, not the instant 10:00 passes). Previously this was
# res.company.grace_time (a separate, user-configurable field defaulting to 15 minutes) -
# replaced with this fixed 20-minute constant, matching the client's explicit "20 minutes"
# spec rather than whatever happens to be configured on the company record.
MZ_ACTIVITY_WINDOW_MINUTES = 20

# Activity card colour thresholds (client instruction, 2026-09-21: "if activity is there for
# a lead, make it light green... 30 mins before activity light yellow, 10 mins before activity
# light orange... red lock happens, and red lead"), minutes BEFORE x_next_activity_datetime -
# see _compute_x_activity_card_state for the full precedence. Replaces the earlier
# purple/green-window scheme (single ±MZ_ACTIVITY_WINDOW_MINUTES band, plus a separate
# "activity today" purple) with this 4-stage countdown instead.
MZ_ACTIVITY_YELLOW_MINUTES = 30
MZ_ACTIVITY_ORANGE_MINUTES = 10

# Which BU pipelines the green/yellow/orange/red activity card state (and the RED lock
# it's built on) applies to at all - client rework spec (2026-09-08): "DMT, Tally, Technology only. Not
# Software Dev, not MIS - they have no Follow-up's stage." Every other team's leads always
# get x_activity_card_state = False ('Normal'), and are excluded from the RED-lock cron
# entirely (_cron_trigger_red_locks) - not just from the colour, the lock itself no longer
# applies there either. TNH added 2026-09-19 (Corporate/LMS/TNH Pipelines build,
# Build Notes #3): "the only Corporate-side pipeline that carries the colour timer and
# RED lock" - Corporate (Hunter/AM/Corp Training) and LMS deliberately do NOT get it,
# despite both having their own "Proposal & Follow-up's"-style stage.
MZ_ACTIVITY_CARD_BU_CATEGORIES = {'dmt', 'tally', 'tech', 'tnh'}

# Corporate BU Manager's actual day-to-day supervision scope (client instruction,
# 2026-09-22: "corp.mgr...should have all access regarding pipeline and also edit
# access, bcoz under his supervision only those 5 teams will come"). corp.mgr is
# only a REAL crm.team.member_ids member of team_corporate itself (just them + 2
# direct agents) - these 5 x_bu_category values are the sub-teams they oversee
# without being literally enrolled in each one's own roster, same as any other
# BU's own Manager oversees their one team by being a genuine member of it.
# Referenced by _mz_is_corp_manager_oversight_team, used everywhere edit/assign
# authority would otherwise require literal team membership or _mz_user_own_team
# equality - kept as x_bu_category values, not crm.team ids, so it stays correct
# even if these teams get renamed/recreated later.
MZ_CORP_MANAGER_BU_CATEGORIES = {'corp_hunter', 'corp_am', 'corp_training', 'lms', 'tnh'}

SYSTEM_FIELDS = {
    "message_follower_ids", "activity_ids", "message_ids", "message_main_attachment_id",
    "website_message_ids", "message_has_error", "message_has_error_counter", "message_needaction",
    "message_needaction_counter", "message_is_follower", "message_partner_ids", "activity_state",
    "activity_user_id", "activity_type_id", "activity_date_deadline", "activity_summary",
    "activity_exception_type", "activity_exception_decoration", "active",
    "x_is_locked", "x_lock_date",
}

# M3: mandatory-field-on-stage-change gate (Mazenet_CRM_M2_Build_Tasks.xlsx's per-stage
# "Mandatory" column). Keyed by crm.team.x_bu_category, one ordered list per BU of
# (stage xmlid, [mandatory field names]) in sequence order - moving a lead FORWARD past
# a stage requires that stage's own fields to already be filled. Only BUs with an entry
# here are gated; teams not yet listed are unaffected.
#
# Two things deliberately excluded from every BU's list below, not missed:
# - "Lost reason" (Won/Lost stage): mandatory only when marking a lead LOST, which is a
#   parallel action (archive + lost_reason/lost_feedback) handled by stock CRM's own Lost
#   wizard, not a forward stage-to-stage move this gate models.
# - Each BU's terminal "Project State" stage's own a/b/c mandatory fields: there is no
#   further stage to advance INTO past it, so "enforce on stage change" has no move left
#   to gate against - these stay reference-only, same as every stage's Mandatory column
#   is explicitly scoped to be until M3 actually implements a trigger for them.
# Deliberate deviation from the sheet: business wants phone/email as an either-or
# pair (fill one, the other stops being required) - NOT what the sheet's own "Any
# one source mandatory" annotation means (that's about the Source field's own
# option list, already satisfied since source_id is a single field), and NOT the
# sheet's "Progressive" wording for Tally's email either (which would have made
# it a plain separate Stage-2 requirement instead).
#
# Unlike every other MZ_STAGE_GATE_RULES entry, phone_or_email is NOT listed
# against any one BU's first stage below - it's checked on EVERY forward stage
# move instead (_mz_stage_gate_check's own universal check, ahead of the
# per-stage loop), since either field can be edited back to blank after the
# first stage passes and a once-only first-stage check would stop catching that.
MZ_EITHER_OR_MANDATORY_FIELDS = {
    'phone_or_email': ('phone', 'email_from'),
    # TNH Stage 3 ("3a plus at least one of 3b-3e - two separate rules on one stage"):
    # x_tnh_meeting_held/attachment_ids are their own always-mandatory entries in
    # MZ_STAGE_GATE_RULES; this pseudo-name covers the SEPARATE "any one service"
    # rule alongside them.
    'x_tnh_service_any': ('x_tnh_service_fte', 'x_tnh_service_cwr', 'x_tnh_service_iaas', 'x_tnh_service_htd'),
    # TNH Stage 5 ("Proposal for - ...", any one mandatory).
    'x_tnh_proposal_any': ('x_tnh_proposal_fte', 'x_tnh_proposal_cwr', 'x_tnh_proposal_iaas', 'x_tnh_proposal_htd'),
}

MZ_STAGE_GATE_RULES = {
    'dmt': [
        ('stage_dmt_new', ['name', 'x_organic_inorganic', 'source_id']),
        ('stage_dmt_contacted', [
            'x_company_or_individual', 'x_contact_purpose', 'x_product_service',
            'x_employee_count', 'x_company_turnover',
        ]),
        ('stage_dmt_qualified', ['x_target_team_id', 'x_transfer_notes']),
        ('stage_dmt_transferred', []),
    ],
    'tally': [
        # Phone/email either-or at Stage 1, same as every other BU (deliberate
        # deviation from the sheet's "Progressive" wording for email - see
        # MZ_EITHER_OR_MANDATORY_FIELDS). email_from is NOT a separate entry at
        # Stage 2 anymore - phone_or_email above already covers it.
        ('stage_tally_new', ['name', 'source_id']),
        ('stage_tally_contacted', ['x_tally_category']),
        ('stage_tally_demo', ['x_requirements_attachment_ids', 'x_product_service', 'x_feasibility', 'x_timeline']),
        ('stage_tally_proposal', ['x_quote_date', 'x_quote_document_ids']),
        ('stage_tally_negotiation', []),
        ('stage_tally_won', []),
        ('stage_tally_lost', []),
    ],
    'tech': [
        ('stage_tech_new', ['name', 'source_id']),
        ('stage_tech_2', ['x_customer_status', 'x_customer_need']),
        ('stage_tech_3', [
            'x_requirements_attachment_ids', 'x_product_service', 'x_feasibility_identified', 'x_timeline',
        ]),
        ('stage_tech_4', [
            'x_bom_attachment_ids', 'x_boq_attachment_ids',
            'x_quote_shared_checkbox', 'x_customer_goods_finalised', 'x_quote_date', 'x_quote_document_ids',
        ]),
        ('stage_tech_5', []),
        ('stage_tech_6', []),
        ('stage_tech_won', []),
    ],
    'swdev': [
        ('stage_swdev_new', ['name', 'source_id']),
        ('stage_swdev_2', [
            'x_branch_count', 'x_sw_employee_count', 'x_nature_of_business',
            'x_established_year', 'x_meeting_attendees',
        ]),
        ('stage_swdev_3', ['x_demo_completed', 'x_system_study_attachment_ids', 'x_feasibility', 'x_timeline']),
        ('stage_swdev_4', ['x_quote_date', 'x_quote_document_ids']),
        ('stage_swdev_5', []),
        ('stage_swdev_won', []),
    ],
    'mis': [
        ('stage_mis_new', ['name', 'source_id']),
        ('stage_mis_2', [
            'x_requirements_attachment_ids', 'x_product_service', 'x_target_audience',
            'x_mis_timelines_estimate', 'x_deliverables',
        ]),
        ('stage_mis_3', ['x_demo_completed', 'x_system_study_attachment_ids', 'x_feasibility', 'x_timeline']),
        ('stage_mis_4', ['x_quote_document_ids']),
        ('stage_mis_5', []),
        ('stage_mis_won', []),
    ],
    # Corporate Pipeline: identical field shape across all 3 teams (Hunter, Account
    # Manager, Corp Training Delivery) - only the stage xmlids differ, since Build
    # Notes #1/#2 require 3 separate stage sets rather than one team_id=False set.
    # Stage 6 (Won/Lost) and Stage 7 (Project State) carry no forward-move gate here
    # deliberately: Won's 3-document gate is enforced separately by action_set_won
    # (MZ_WON_GATE_RULES) since it's a close-action check, not a "next stage" one, and
    # Project State is the pipeline's terminal stage (same convention as every other
    # BU's own Project State/Won stage above).
    'corp_hunter': [
        ('stage_corp_hunter_new', ['name', 'source_id']),
        ('stage_corp_hunter_validation', [
            'x_client_expectations_attachment_ids', 'x_product_service', 'x_target_audience',
            'x_lead_timelines_days', 'x_deliverables',
        ]),
        ('stage_corp_hunter_deck', ['x_presentation_completed_datetime']),
        ('stage_corp_hunter_proposal', ['x_quote_document_ids']),
        ('stage_corp_hunter_evaluation', [
            'x_training_content_eval_start_date', 'x_training_content_finalized_date',
            'x_trainer_eval_start_date', 'x_trainer_eval_finalized_date',
            'x_training_dates_finalized',
        ]),
        ('stage_corp_hunter_won', []),
        ('stage_corp_hunter_project_state', []),
    ],
    'corp_am': [
        ('stage_corp_am_new', ['name', 'source_id']),
        ('stage_corp_am_validation', [
            'x_client_expectations_attachment_ids', 'x_product_service', 'x_target_audience',
            'x_lead_timelines_days', 'x_deliverables',
        ]),
        ('stage_corp_am_deck', ['x_presentation_completed_datetime']),
        ('stage_corp_am_proposal', ['x_quote_document_ids']),
        ('stage_corp_am_evaluation', [
            'x_training_content_eval_start_date', 'x_training_content_finalized_date',
            'x_trainer_eval_start_date', 'x_trainer_eval_finalized_date',
            'x_training_dates_finalized',
        ]),
        ('stage_corp_am_won', []),
        ('stage_corp_am_project_state', []),
    ],
    'corp_training': [
        ('stage_corp_training_new', ['name', 'source_id']),
        ('stage_corp_training_validation', [
            'x_client_expectations_attachment_ids', 'x_product_service', 'x_target_audience',
            'x_lead_timelines_days', 'x_deliverables',
        ]),
        ('stage_corp_training_deck', ['x_presentation_completed_datetime']),
        ('stage_corp_training_proposal', ['x_quote_document_ids']),
        ('stage_corp_training_evaluation', [
            'x_training_content_eval_start_date', 'x_training_content_finalized_date',
            'x_trainer_eval_start_date', 'x_trainer_eval_finalized_date',
            'x_training_dates_finalized',
        ]),
        ('stage_corp_training_won', []),
        ('stage_corp_training_project_state', []),
    ],
    # LMS Pipeline: Stages 1-4 reuse Corporate's own field shape. Stage 5 (Won/Lost)
    # and Stage 8 (Project State) carry no forward-move gate, same reasoning as
    # Corporate above. Stage 6 (Delivery) is gated; Stage 7 (Content Availability) is
    # NOT - Build Notes #5/#6 call it "the hardest stage in the project" precisely
    # because its content is per-week child rows (x_lms_week_ids), not plain fields a
    # name-list gate can check, so it's left to the Friday cron/escalation instead of
    # a stage-advance block.
    'lms': [
        ('stage_lms_new', ['name', 'source_id']),
        ('stage_lms_validation', [
            'x_client_expectations_attachment_ids', 'x_product_service', 'x_target_audience',
            'x_lead_timelines_days', 'x_deliverables',
        ]),
        ('stage_lms_deck', ['x_presentation_completed_datetime']),
        ('stage_lms_proposal', ['x_quote_document_ids']),
        ('stage_lms_won', []),
        ('stage_lms_delivery', [
            'x_lms_training_content', 'x_lms_toc_by', 'x_lms_content_availability',
            'x_lms_training_start_date', 'x_lms_training_end_date',
        ]),
        ('stage_lms_content_availability', []),
        ('stage_lms_project_state', []),
    ],
    # TNH Pipeline: the only Corporate-side pipeline with the colour timer/RED lock
    # (Stage 6, Follow-up's - see MZ_ACTIVITY_CARD_BU_CATEGORIES), and the only one
    # with a genuinely standalone Project State (no Training Status stage to fold it
    # into, unlike Corporate/LMS). Stage 6 and Stage 8 (Won/Lost) carry no field gate,
    # same reasoning as every other BU's Follow-up's/Won stage above.
    'tnh': [
        ('stage_tnh_new', ['name', 'source_id']),
        ('stage_tnh_2', ['x_company_turnover', 'x_employee_count', 'x_nature_of_business']),
        ('stage_tnh_3', ['x_tnh_meeting_held', 'x_tnh_meeting_attachment_ids', 'x_tnh_service_any']),
        ('stage_tnh_4', ['x_presentation_completed_datetime']),
        ('stage_tnh_5', ['x_tnh_proposal_any', 'x_tnh_deviation_text']),
        ('stage_tnh_6', []),
        ('stage_tnh_7', ['x_tnh_agreement_doc_ids']),
        ('stage_tnh_won', []),
        ('stage_tnh_project_state', []),
    ],
}

# Corporate Evaluation stage (Stage 5): a field normally mandatory to advance past
# Evaluation is waived when its paired checkbox is ticked - Pre-Approved skips
# Training Content Finalized, Mazenet Validation skips Trainer Evaluation Finalized.
# Kept as its own dict (not folded into MZ_EITHER_OR_MANDATORY_FIELDS, which means
# "any one of several alternatives", a different rule) so _mz_missing_mandatory_fields
# can special-case it the same explicit way.
MZ_WAIVER_CONDITIONAL_MANDATORY_FIELDS = {
    'x_training_content_finalized_date': 'x_training_content_preapproved',
    'x_trainer_eval_finalized_date': 'x_trainer_mazenet_validated',
}

# LMS Delivery stage (Stage 6): x_lms_toc_by only applies (and is only mandatory)
# when its companion Selection field holds a SPECIFIC value - a different shape from
# MZ_WAIVER_CONDITIONAL_MANDATORY_FIELDS above (that one waives on a boolean being
# True; this one requires on a Selection equalling one particular option, per the
# sheet's own "Only applies when 6a = New Content" wording).
MZ_SELECTION_CONDITIONAL_MANDATORY_FIELDS = {
    'x_lms_toc_by': ('x_lms_training_content', 'new_content'),
}

# TNH Stage 5 ("Deviation... text box is mandatory ONLY IF the Deviation checkbox is
# ticked"): the OPPOSITE direction from MZ_WAIVER_CONDITIONAL_MANDATORY_FIELDS above
# (that one drops the requirement when its boolean is True; this one ADDS the
# requirement when its boolean is True) - kept as its own dict rather than
# overloading the waiver one with a "sense" flag, since the two are checked by
# fully separate branches in _mz_missing_mandatory_fields.
MZ_BOOLEAN_CONDITIONAL_MANDATORY_FIELDS = {
    'x_tnh_deviation_text': 'x_tnh_deviation',
}

# Corporate Won/Lost gate (Stage 6): action_set_won below blocks Won unless every
# listed attachment field is filled - Corporate's is the project's only THREE-
# document gate (tagged Quotation from Stage 4, plus both POs from Stage 6). LMS's
# own Won gate document was an open client question - built with the sheet's own
# proposed default (a tagged Quotation), a single-document gate. TNH's is a TWO-
# document gate (tagged Quotation/Proposal, plus the Stage 7 NDA/confirmation doc).
MZ_WON_GATE_RULES = {
    'corp_hunter': ['x_quote_document_ids', 'x_po_received_attachment_ids', 'x_po_issued_trainer_attachment_ids'],
    'corp_am': ['x_quote_document_ids', 'x_po_received_attachment_ids', 'x_po_issued_trainer_attachment_ids'],
    'corp_training': ['x_quote_document_ids', 'x_po_received_attachment_ids', 'x_po_issued_trainer_attachment_ids'],
    'lms': ['x_quote_document_ids'],
    'tnh': ['x_quote_document_ids', 'x_tnh_agreement_doc_ids'],
}

class CrmLead(models.Model):
    _inherit = "crm.lead"

    x_next_activity_datetime = fields.Datetime(
        string="Next Activity Time",
        compute="_compute_x_next_activity_datetime",
        store=True,
        index=True,
        help="Earliest open activity's actual moment, resolved in this order: (1) a linked "
             "calendar event's real start time, for Meeting-category activities; (2) the "
             "activity's own mz_activity_time combined with its due date, for Call/To-Do "
             "activities that were given a time; (3) the due date at a default hour "
             "(MZ_DEFAULT_ACTIVITY_HOUR), for activities with no time source at all. Drives "
             "the Pipeline KANBAN's card order (soonest activity on top; leads with no open "
             "activity naturally sort to the bottom - NULL last on ASC) via that view's own "
             "default_order (views/crm_lead_views.xml) - deliberately NOT crm.lead's model-"
             "level _order, which would apply this ordering to every list/pivot/calendar view "
             "of leads system-wide, not just the one Pipeline kanban it's meant for. Stored "
             "(not a plain compute) specifically so it CAN be used to order a kanban at all."
    )

    is_show_redlock_btn = fields.Boolean(
        string="Show Release RED Lock Button", copy=False, store=False,
        compute="_compute_show_redlock",
        help="Whether the CURRENT user (viewing/editing this lead right now) should see "
             "the 'Release RED Lock' button - True only when the lead is actually locked "
             "(x_is_locked) AND can_user_release_lock() authorizes them. Not stored -"
             " reflects whoever has the form open, same as x_content_readonly_for_me.\n"
             "Client bug report (2026-09-21): 'when i create lead and save it im seeing "
             "button release red lock' - the previous version of this compute never "
             "checked x_is_locked at all, only 'is the current user this lead's TEAM's "
             "own user_id field (crm.team.user_id, an entirely different field from the "
             "lead's owner) OR CTO/Admin/MD' - so a brand-new, never-locked lead still "
             "showed the button for anyone in either of those two buckets. Fixed by "
             "reusing can_user_release_lock() (the actual authorization check "
             "action_release_lock() itself already enforces), gated on x_is_locked."
    )

    @api.depends('x_is_locked')
    @api.depends_context('uid')
    def _compute_show_redlock(self):
        for lead in self:
            lead.is_show_redlock_btn = bool(lead.x_is_locked) and lead.can_user_release_lock()


    @api.model
    def _mz_user_own_team(self, user=None):
        """The crm.team `user` is a direct member of, resolved from
        crm.team.member_ids - the relation mazenet_crm's own access-control logic
        actually uses everywhere (_mz_can_edit_by_team, _mz_can_edit_owned,
        record_rules.xml's team-scoped rules, etc.). Deliberately NOT
        res.users.crm_team_ids: that's stock Odoo's OWN, separate team-membership
        mechanism (computed from crm.team.member join records - sales_team's
        res_users.py), which demo data never populates here, so it's empty for
        every user in this project and silently wrong for this purpose. Assumes
        one team per user, which matches how every mazenet_access_rights role is
        actually set up; returns an empty recordset if none/ambiguous."""
        user = user or self.env.user
        return self.env['crm.team'].search([('member_ids', '=', user.id)], limit=1)

    @api.model
    def _mz_is_corp_manager_oversight_team(self, user, team):
        """Whether `team` is one of the 5 Corporate sub-teams
        (MZ_CORP_MANAGER_BU_CATEGORIES) that `user`, as Corporate BU Manager,
        oversees without being a literal crm.team.member_ids member of it -
        client instruction, 2026-09-22: corp.mgr "should have all access
        regarding pipeline and also edit access, bcoz under his supervision
        only those 5 teams will come". _mz_user_own_team(corp.mgr) itself still
        only ever resolves to team_corporate (their one real membership) -
        this is a SEPARATE, additional check, used ALONGSIDE (never instead
        of) every place that otherwise gates edit/assign authority on literal
        team.member_ids membership or _mz_user_own_team equality
        (_mz_can_edit_by_team, _mz_can_edit_owned, _compute_x_assignable_user_ids's
        can_assign_within_own_team, _mz_check_assign_type_allowed's
        own_team_internal_ok, and assign_salesperson's 'internal' onchange) -
        keep all of them in sync."""
        return bool(
            team
            and team.x_bu_category in MZ_CORP_MANAGER_BU_CATEGORIES
            and user.has_group('mazenet_access_rights.group_mzr_corporate_manager')
        )

    @api.model
    def _mz_user_is_dmt(self, user=None):
        """Whether `user` (default: current user) is a direct member of the DMT team
        (_mz_user_own_team). Shared by every DMT waiver in this file - the assignable-
        pool compute, the create()/write() pool backstop (_mz_check_assign_type_allowed),
        and the write() RED-lock/team-transfer gate - so they can't drift out of sync."""
        user = user or self.env.user
        dmt_team = self.env.ref('mazenet_crm.team_dmt', raise_if_not_found=False)
        return bool(dmt_team) and self._mz_user_own_team(user) == dmt_team

    @api.model
    def _mz_user_can_use_assign_radio(self, user=None):
        """Whether `user` (default: current user) may interact with the Assign Type
        radio (x_assign_type) at all: DMT team membership (any tier - same waiver
        as _mz_user_is_dmt), or one of the two global oversight roles, MD or
        CTO/Admin. Every other team's Agent/ATL/TL/Manager is Self-only now -
        'Team'/'Internal' delegation used to be a per-team ATL/TL/Manager
        privilege (see _compute_x_assignable_user_ids's docstring history); it's
        now restricted to DMT plus the two global roles, and the radio itself is
        readonly in the view for everyone else so they can't even attempt it."""
        user = user or self.env.user
        return (
            self._mz_user_is_dmt(user)
            or user.has_group('mazenet_access_rights.group_mzr_cto_admin')
            or user.has_group('mazenet_access_rights.group_mzr_md')
        )

    @api.model
    def _mz_default_team_id(self):
        """Wired back as team_id's field default 2026-09-12 (client instruction, after a
        brief stint - 2026-09-12 same day - with NO default at all): prefill should
        happen ONLY for whoever actually has an own team to prefill with (a DMT member's
        new lead starts on DMT, a Tally member's starts on Tally, etc, via
        _mz_user_own_team) - the earlier "no default" fix was really only needed to stop
        CTO/Admin/MD (who own no team) from getting silently defaulted to DMT the instant
        they created a lead. That CTO/Admin/MD fallback-to-DMT branch is gone for good now
        - they get a genuinely empty team_id and are expected to route the lead via the
        'Internal' assign type instead (_default_x_assign_type defaults them there), which
        forces an explicit team pick through team_id's own required="x_assign_type !=
        'self'" in the view.

        Still doubles as create()'s own last-resort fallback (see create()'s
        No-Teamless-Lead Guarantee below) for creation paths that never went through the
        form at all - import, API, incoming email - where there's no view-level required
        check to rely on; for a CTO/Admin/MD/no-team caller going through one of those
        paths, this now correctly returns False and lets create()'s own DMT catch-all
        (a SEPARATE, deliberate safety net - see its comment) take over instead."""
        own_team = self._mz_user_own_team()
        return own_team.id if own_team else False

    @api.depends('team_id', 'type')
    def _compute_stage_id(self):
        """Stock's own version (addons/crm/models/crm_lead.py) always assigns a
        fallback stage via _stage_find even with NO team_id at all - _stage_find
        with team_id=False still matches whatever globally-unscoped (or just the
        first) crm.stage row exists, misleadingly showing a real Stage on the
        statusbar for a lead that doesn't actually belong to any team's pipeline
        yet. CTO/Admin's own team_id now genuinely starts (and can stay) empty
        (2026-09-12/2026-09-14 client instruction: "no pipeline for CTO, keep
        empty") - stage_id should match that until a real team is actually
        picked, same as team_id itself already does. Only skips the stock
        fallback for leads with NO team_id; every other lead behaves exactly as
        stock CRM already did."""
        with_team = self.filtered('team_id')
        super(CrmLead, with_team)._compute_stage_id()
        for lead in self - with_team:
            lead.stage_id = lead.stage_id or False

    @api.model
    def _mz_team_entry_stage(self, team):
        """The first (lowest-sequence) real stage scoped to `team` - used to advance a
        handed-off lead's REAL stage_id into the RECEIVING team's own pipeline at the
        moment of a cross-team handoff (2026-09-12, write()'s Cross-Team Handoff Stage
        Advance), instead of leaving it parked on whichever stage it came from - which,
        being scoped to the OLD team, would leak in as a phantom column on the new
        team's kanban the instant a real record sits in it (_read_group_stage_ids's own
        team-filter below only hides EMPTY foreign columns, never ones with a real
        record already in them - see its own docstring). Empty recordset if the team
        has no stages of its own at all."""
        if not team:
            return self.env['crm.stage']
        return self.env['crm.stage'].search(
            [('team_ids', 'in', team.id)], order='sequence asc', limit=1
        )

    def _mz_resolve_stage_team_id_from_domain(self, domain):
        """Sales Team AND Salesperson search panel selections should both drive
        the Pipeline kanban's stage columns the same way (2026-09-04: "the same
        [as Sales Team] should follow ... for salesperson filter also"). Reads
        the selected team_id/user_id straight off the domain's own leaves
        (Domain.iter_conditions(), so it works whether `domain` arrives as a
        plain list or an already-parsed Domain object) rather than deriving it
        from matching lead records (_read_group(domain, ['team_id']), the
        original approach) - that broke for a team/salesperson with ZERO leads
        currently matching, since a group-by naturally returns no groups for
        an empty result set even though the selection itself is unambiguous.
        team_id wins if both are somehow present; user_id resolves via
        res.users.x_mz_team_id (the same field the Salesperson section's own
        groupby uses). None if neither is selected (the "All" view).

        A DomainCondition's 'in' value isn't reliably a plain list/tuple/set -
        confirmed 2026-09-08 via live debug logging: the real search panel
        selection arrives as ('team_id', 'in', OrderedSet([40])), and
        OrderedSet (odoo.tools.misc) is NOT a subclass of the builtin set (it's
        a collections.abc.MutableSet), so an isinstance(value, (list, tuple,
        set)) check silently failed to unwrap it - team_id was never found,
        and every CTO/MD team selection quietly fell back to DMT, leaking
        DMT's stages into whatever team was actually picked. Checking
        Iterable instead (rather than trying to enumerate every container
        type Odoo domains might use) is what actually holds up here."""
        from collections.abc import Iterable
        from odoo.orm.domains import Domain
        team_id = None
        user_id = None
        for cond in Domain(domain).iter_conditions():
            value = cond.value
            if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
                value = next(iter(value), None)
            if not isinstance(value, int) or isinstance(value, bool):
                continue
            if cond.field_expr == 'team_id' and cond.operator in ('=', 'in'):
                team_id = value
            elif cond.field_expr == 'user_id' and cond.operator in ('=', 'in'):
                user_id = value
        if team_id:
            return team_id
        if user_id:
            return self.env['res.users'].browse(user_id).x_mz_team_id.id
        return None

    @api.model
    def _read_group_stage_ids(self, stages, domain):
        """CTO/Admin and MD have no crm.team of their own (_mz_user_own_team is
        empty), so the stock implementation's own-team stage columns
        (self.env.context['default_team_id']) never kick in for them and their
        Pipeline kanban - most visibly "My Pipeline", since My Pipeline's action
        sets no default_team_id at all - shows no stage columns until they
        happen to own a lead in one. Inject a team_id into context so they see
        a proper stage set, matching where _mz_default_team_id above now routes
        their own new leads. Skipped when a team-specific menu already set
        default_team_id, so per-team Pipeline menus are unaffected.

        Which team to inject is resolved from `domain` itself
        (_mz_resolve_stage_team_id_from_domain) - fixed 2026-09-04: clicking a
        specific team (or now, salesperson) in the sidebar kept showing DMT's
        stage columns regardless, because this used to force default_team_id=
        DMT unconditionally. DMT is only the fallback when nothing is selected
        (the "All" view).

        Also strips show_user_team_stages from context - fixed 2026-09-08:
        crm.crm_lead_action_pipeline (the stock Pipeline action) always sets
        show_user_team_stages=1, which makes the super() call ALSO unconditionally
        OR in self.env.user.crm_team_ids regardless of our own default_team_id
        override. Whenever a CTO/Admin/MD happens to be a member/leader of some
        team too (crm_team_ids is res_users.py's OWN Many2many, separate from
        our res.users.x_mz_team_id / _mz_user_own_team), THAT team's stages kept
        leaking in on top of whichever team was actually selected - reported as
        an extra "New Lead / Source" (DMT) column bleeding into MIS's own "New
        Lead" one - this alone wasn't the full story though, see
        _mz_resolve_stage_team_id_from_domain's own docstring for the other
        half (an OrderedSet unwrapping bug that made team/salesperson
        selection silently fall back to DMT every time). Since we're already
        resolving the correct team ourselves here, that extra OR only ever
        reintroduces stale/wrong columns.

        Second leak, same symptom, different cause (hit live 2026-09-11, DMT
        Agent's Pipeline once the 'My Pipeline' filter was removed): DMT keeps
        READ access to a lead it originated even after it's transferred to
        another team (rule_crm_lead_dmt_originated_read, x_dmt_originated) -
        intentional, so the handoff doesn't lock the originating DMT user out
        entirely. But stock's own _read_group_stage_ids (see its
        'id in stages.ids' clause below) adds a column for ANY stage that has
        at least one currently-VISIBLE record, with no team check at all - so
        the instant one such transferred-out lead is visible, its CURRENT
        stage (which belongs to the OTHER team, e.g. MIS's own 'New Lead')
        shows up as an extra column mixed into DMT's kanban. Narrowing the
        search (the 'My Pipeline' filter) just happened to keep that record
        out of the read_group's matches; it was never actually team-scoped.
        Fixed the same way for every user, not just CTO/MD: once the team
        this Pipeline actually belongs to is known (from context or the
        viewer's own team), strip the result down to that team's own stages
        (plus genuinely global, team_ids=False ones) - a stage only present
        because of some OTHER cross-cutting read grant doesn't belong here.

        Corporate BU Manager added 2026-09-22 (client bug report: "in kanban
        also all pipeline stages should show, now only one stage showing...
        but for cto all stages showing same like that need for corp.mgr") -
        corp.mgr oversees 5 DIFFERENT teams (Hunter/AM/Corp Training/LMS/TNH,
        see MZ_CORP_MANAGER_BU_CATEGORIES) without _mz_user_own_team(corp.mgr)
        ever resolving to any of them (that stays team_corporate, their one
        real crm.team.member_ids membership) - so before this fix, picking
        e.g. "LMS" in the Sales Team panel still forced target_team_id back to
        team_corporate regardless, and the filter below then stripped out
        every genuine LMS-only stage column, leaving just whatever stage(s)
        happened to already hold a visible record. Needs the SAME
        domain-based resolution as CTO/Admin/MD, since they likewise view
        more than one team's pipeline depending on what's selected - but
        UNLIKE CTO/Admin/MD (who own no team at all), corp.mgr falls back to
        their own real team (team_corporate) rather than an empty stage set
        when nothing is selected (the "All" view), since that's a genuine,
        sensible default for them."""
        target_team_id = self.env.context.get('default_team_id')
        if not target_team_id:
            user = self.env.user
            is_cto_or_md = (
                user.has_group('mazenet_access_rights.group_mzr_cto_admin')
                or user.has_group('mazenet_access_rights.group_mzr_md')
            )
            is_corp_manager = user.has_group('mazenet_access_rights.group_mzr_corporate_manager')
            if is_cto_or_md or is_corp_manager:
                target_team_id = self.sudo()._mz_resolve_stage_team_id_from_domain(domain)
                if not target_team_id:
                    if is_corp_manager:
                        target_team_id = self._mz_user_own_team(user).id or None
                    else:
                        # CTO/Admin and MD: no fallback team for the "All" view for
                        # now (client instruction, 2026-09-14 for CTO, extended
                        # 2026-09-15 to MD: "for cto we removed pipeline right,
                        # same remove pipeline stages stage_id for md as well") -
                        # neither owns a team of their own, and defaulting to
                        # DMT's stage columns implied they were a DMT member, the
                        # same reasoning that already stopped team_id/x_assign_type
                        # from defaulting to DMT for them elsewhere in this file.
                        # Real records still group by their own actual stage
                        # regardless (group_expand can't suppress that) - this
                        # only drops the synthetic EMPTY DMT columns.
                        return self.env['crm.stage']
            else:
                target_team_id = self._mz_user_own_team(user).id or None
            if target_team_id:
                self = self.with_context(
                    default_team_id=target_team_id, show_user_team_stages=False
                )
        result = super()._read_group_stage_ids(stages, domain)
        if target_team_id:
            result = result.filtered(lambda s: not s.team_ids or target_team_id in s.team_ids.ids)
        return result

    def _read_group(self, domain, groupby=(), aggregates=(), having=(), offset=0, limit=None, order=None):
        """Excludes a lead visible ONLY via a cross-team read grant like
        x_dmt_originated from being counted under its own real, FOREIGN stage when
        grouping the generic Pipeline by the real stage_id (2026-09-12, hit live: a
        lead DMT handed off to Tally showed up as a stray "New Lead" column - Tally's
        own real stage - on DMT's OWN generic Pipeline, right alongside DMT's own real
        stages). group_expand (_read_group_stage_ids above) can only hide EMPTY
        foreign columns, never one a real, currently-visible record is actually
        sitting in - so the only way to keep such a record out of THIS kanban/pivot/
        graph grouping is to keep it out of the read_group's own domain entirely.
        DMT's OWN dedicated Pipeline groups by x_dmt_pipeline_stage_id instead (a
        different field - see that field's help text) and is unaffected; a lead only
        stays excluded here until the receiving team's first edit self-expires
        x_dmt_originated (see write()), after which it was never going to be DMT's
        concern in either view anyway.

        Skipped for CTO/Admin/MD - their whole-company overview legitimately needs to
        see every team's records regardless of who "owns" them."""
        if (
            groupby and groupby[0] == 'stage_id'
            and not self.env.su
            and not self.env.user.has_group('mazenet_access_rights.group_mzr_cto_admin')
            and not self.env.user.has_group('mazenet_access_rights.group_mzr_md')
        ):
            target_team_id = self.env.context.get('default_team_id') or self._mz_user_own_team().id
            if target_team_id:
                from odoo.orm.domains import Domain
                domain = Domain.AND([
                    domain,
                    Domain(['|', ('x_dmt_originated', '=', False), ('team_id', '=', target_team_id)]),
                ])
        return super()._read_group(domain, groupby, aggregates, having, offset, limit, order)

    # "Exclude the lead's own current team from its own team-picker" (2026-09-15 fix:
    # "when transfering lead to other team from dmt in dropdown dmt team also
    # showing, which should not happen") used to live here as a Python domain=
    # callable (_get_team_id_domain, removed 2026-09-21) - it LOOKED correct
    # (self.team_id.ids resolves fine when called directly on a real record, see
    # the very next line's own field access) but fields_get() - what actually
    # supplies the web client's dropdown search domain - calls it against an
    # EMPTY recordset, so self.team_id.ids was always [] and it excluded nothing,
    # ever, in the live UI (confirmed: this is the SAME bug the 2026-09-15 fix
    # was originally meant to close, just relocated - the version it replaced had
    # the identical problem via self.env.user.crm_team_ids, an always-empty field
    # for a different reason). Real fix now lives in the VIEW itself
    # (crm_lead_views.xml), as domain="[('id', '!=', team_id)]" on team_id's and
    # x_target_team_id's own field tags - a view-level domain string referencing
    # another field on the SAME record by name is evaluated against the actual
    # loaded form values, not an empty model-level recordset.
    team_id = fields.Many2one(
        "crm.team",
        default=_mz_default_team_id,
    )

    x_related_lead_id = fields.Many2one(
        'crm.lead', string="Related Lead", readonly=True, copy=False,
        help="The lead this one was spun off from via 'Create New Opportunity' - same "
             "customer, a different requirement routed to (usually) a different "
             "Sales Team. Set once at creation by the wizard, never editable "
             "afterwards."
    )
    x_spinoff_lead_ids = fields.One2many(
        'crm.lead', 'x_related_lead_id', string="Spin-off Leads",
        help="Leads created FROM this one via 'Create New Opportunity' - same customer, "
             "a different requirement routed to another team/salesperson."
    )

    x_dmt_originated = fields.Boolean(
        string="Originated From DMT", copy=False,
        help="Set once, at creation, if the lead's team_id was DMT at the time - "
             "never changed afterwards even if team_id later moves elsewhere. "
             "Exists purely so rule_crm_lead_dmt_originated_read "
             "(security/record_rules.xml) can give DMT read-only visibility on a "
             "lead they originally handled, even after handing it off to another "
             "team via the 'Team' assign-type radio - DMT's own pipeline has a "
             "'Follow-up's' stage AFTER 'Transfer to BU', so losing all read "
             "access the instant team_id changes broke that follow-up step (and "
             "surfaced as an AccessError on the very save that performed the "
             "handoff, since the client's own post-write re-read hit the same "
             "now-out-of-scope domain) - fixed 2026-09-08."
    )
    x_dmt_pipeline_stage_id = fields.Many2one(
        'crm.stage', string="DMT Pipeline Stage",
        compute='_compute_x_dmt_pipeline_stage_id', store=True,
        inverse='_inverse_x_dmt_pipeline_stage_id',
        group_expand='_read_group_stage_ids',
        help="DMT's OWN dedicated Pipeline kanban (view_crm_lead_kanban_dmt_pipeline / "
             "crm_lead_action_pipeline_dmt) groups AND drags by THIS field instead of "
             "the real stage_id (2026-09-12) - mirrors stage_id normally, but pins to "
             "DMT's own 'Follow-up's' stage (stage_dmt_transferred) once "
             "x_dmt_originated is set and the lead has moved to another team's real "
             "pipeline. Dragging a card in that kanban writes here (whatever field a "
             "kanban is grouped by is what drag-and-drop writes to), and the inverse "
             "(guarded the same way _inverse_x_assign_type_no_internal is, against the "
             "same recursive-write footgun) converts that into a real stage_id write -"
             " so for a lead STILL on DMT (this field mirrors stage_id 1:1 there),  "
             "dragging genuinely progresses it, M3 gate and all, same as the standard "
             "Pipeline. Dragging a lead that's ALREADY been handed off (this field "
             "pinned to Follow-up's) still can't actually move it anywhere - write()'s "
             "own existing 'transferred, read-only' guard rejects it before the "
             "inverse ever runs, since team_id is no longer DMT's. Reuses "
             "_read_group_stage_ids as its own group_expand so DMT's real stage "
             "columns (including the otherwise-unreachable-by-handoff 'Follow-up's' "
             "one) still show up empty rather than only appearing once a lead happens "
             "to land there."
    )

    @api.depends('stage_id', 'team_id', 'x_dmt_originated')
    def _compute_x_dmt_pipeline_stage_id(self):
        dmt_team = self.env.ref('mazenet_crm.team_dmt', raise_if_not_found=False)
        stage_followup = self.env.ref('mazenet_crm.stage_dmt_transferred', raise_if_not_found=False)
        for lead in self:
            if dmt_team and stage_followup and lead.x_dmt_originated and lead.team_id != dmt_team:
                lead.x_dmt_pipeline_stage_id = stage_followup
            else:
                lead.x_dmt_pipeline_stage_id = lead.stage_id

    def _inverse_x_dmt_pipeline_stage_id(self):
        """See the field's own help text for the full drag-and-drop mechanics. Guarded
        to a genuine no-op when the value already matches, same as
        _inverse_x_assign_type_no_internal - without this guard, EVERY write touching
        this field (not just a drag) would trigger a needless recursive stage_id
        write."""
        for lead in self:
            if lead.stage_id != lead.x_dmt_pipeline_stage_id:
                lead.stage_id = lead.x_dmt_pipeline_stage_id

    @api.model
    def _default_x_assign_type(self):
        """Agents can't use 'team' or 'internal' (see x_can_assign_beyond_self), so
        defaulting everyone to 'team' meant every Agent got bounced back to 'self'
        with a warning on every single new lead. Pick the default from the current
        user's own tier instead, so an Agent starts on 'self' - the only option
        that was ever going to stick for them - and TL/ATL/Manager keep the
        original 'team' default.

        'Team'/'Internal' default is now ALSO gated on _mz_user_can_use_assign_radio
        (DMT/CTO/Admin/MD only) - fixed 2026-09-08: a non-DMT Team Lead/Manager
        (e.g. Tally TL) still qualified for the tier check below on its own, so
        Kanban quick-create defaulted x_assign_type='team' for them even though
        _mz_check_assign_type_allowed has rejected 'team' for anyone outside
        DMT/CTO/Admin for a while now - the create() call then hit that very
        AccessError on a plain quick-create, before the user ever touched the
        (correctly readonly-forced-to-self) radio on the full form.

        CTO/Admin and MD always start a brand-new lead on 'Self' (2026-09-12, client
        instruction, corrected same day from a brief 'Internal' default): checked
        BEFORE the tier fallback below on purpose - group_mzr_cto_admin's own
        implied_ids include every team's Manager group, and has_group() (which
        _mz_user_tier_chain relies on) resolves implied groups transitively even
        though CTO/Admin never actually gets a real row in that group's membership
        - so the tier-chain fallback would otherwise misread CTO/Admin as a genuine
        DMT Manager and default them to 'team' immediately, which is the exact bug
        that started this whole conversation ("cto tries to create new lead it auto
        assigns to team"). 'Team' is still reachable for CTO/Admin, just only AFTER
        the lead is saved (see x_hide_internal_option/x_show_internal_only for what
        it looks like once picked) - 'Internal', in turn, only ever applies to an
        EXISTING lead already owned by someone else, never to a brand-new one."""
        user = self.env.user
        if not self._mz_user_can_use_assign_radio(user):
            return 'self'
        if (
            user.has_group('mazenet_access_rights.group_mzr_cto_admin')
            or user.has_group('mazenet_access_rights.group_mzr_md')
        ):
            return 'self'
        tier, _chain = self._mz_user_tier_chain(user)
        return 'team' if tier in ('atl', 'tl', 'manager') else 'self'

    @api.model
    def _selection_x_assign_type(self):
        """Always all three - who may actually USE anything beyond 'Self' is separately
        gated by x_can_use_assign_radio/x_can_assign_beyond_self, and (2026-09-08) whether
        CTO/Admin/MD specifically see 'Internal' AT ALL on a given lead is a PER-RECORD
        question (hidden on their own already-saved lead, shown otherwise) that a
        Selection field's static, per-environment-only option list can't answer - see
        x_hide_internal_option and x_assign_type_no_internal for how that's actually
        done."""
        return [('self', 'Self'), ('internal', 'Internal'), ('team', 'Team')]

    x_assign_type = fields.Selection(
        selection='_selection_x_assign_type',
        string="Assign Type", default=_default_x_assign_type,
        help="How user_id gets populated:\n"
             "- Self: always the current user. Available to everyone.\n"
             "- Team: hand-picked from the selected team's 'Create To' users.\n"
             "- Internal: hand-picked from whoever's directly in a group ranked below "
             "yours in the team's configured privilege/group hierarchy (DMT: any team "
             "member, no restriction). For CTO/Admin/MD specifically, hidden in the view "
             "(x_assign_type_no_internal shown instead) on a lead they already own - see "
             "x_hide_internal_option.\n"
             "'Team' and 'Internal' are only offered to Team Leads, ATLs and BU Managers "
             "(mazenet_access_rights) - an Agent has no one to delegate to, so both are "
             "restricted to Self for them."
    )
    x_hide_internal_option = fields.Boolean(
        compute='_compute_x_assignable_user_ids',
        string="Hide Internal Option",
        help="CTO/Admin and MD only: True when viewing a lead THEY ALREADY OWN "
             "(lead.id is set - a genuinely new, unsaved lead is never 'owned' yet even "
             "though user_id defaults to the creator - and user_id == the current user). "
             "Drives which of x_assign_type (all 3 options) / x_assign_type_no_internal "
             "(Self/Team only) is shown in the view - 'Internal' makes sense for CTO/MD "
             "delegating someone ELSE's lead, not their own. Not stored - reflects "
             "whoever has the form open."
    )
    x_assign_type_no_internal = fields.Selection(
        [('self', 'Self'), ('team', 'Team')],
        string="Assign Type", compute='_compute_x_assign_type_no_internal',
        inverse='_inverse_x_assign_type_no_internal',
        help="Mirror of x_assign_type with 'Internal' left out entirely (not just hidden -"
             " a Selection field's option list is per-environment, not per-record, so "
             "hiding one choice on some leads and not others needs a second field with "
             "its own static, narrower option list, shown INSTEAD of the real one via "
             "x_hide_internal_option). Reads/writes the exact same underlying value as "
             "x_assign_type; exists purely for this view swap, not a separate concept."
    )
    x_show_internal_only = fields.Boolean(
        compute='_compute_x_assignable_user_ids',
        string="Show Internal Only",
        help="CTO/Admin only (2026-09-12): True when viewing an EXISTING lead owned by "
             "someone else - the only sensible action there is 'Internal' (reassigning "
             "within that lead's own team hierarchy); 'Self' would self-assign someone "
             "else's lead and 'Team' would re-route it entirely, neither of which is what "
             "CTO/Admin opening someone else's lead is for. Drives x_assign_type_internal_only "
             "being shown instead of x_assign_type_no_internal. MD never sees this - MD's "
             "x_can_assign_beyond_self is always False, so the radio is readonly-forced-to-"
             "Self for them regardless of which mirror field is technically in the view."
    )
    x_assign_type_internal_only = fields.Selection(
        [('internal', 'Internal')],
        string="Assign Type", compute='_compute_x_assign_type_internal_only',
        inverse='_inverse_x_assign_type_internal_only',
        help="Second mirror of x_assign_type (see x_assign_type_no_internal), shown INSTEAD "
             "of both other variants when x_show_internal_only is True - CTO/Admin viewing "
             "an existing lead owned by someone else only ever has one meaningful choice, so "
             "unlike x_assign_type_no_internal (a real 2-way choice), this one's whole "
             "option list is just the single value it's already forced to."
    )
    x_hide_team_option = fields.Boolean(
        compute='_compute_x_assignable_user_ids',
        string="Hide Team Option",
        help="True for a regular team's Agent/ATL/TL/Manager - anyone who is NOT DMT, "
             "CTO/Admin or MD (2026-09-15, client instruction: 'team radio button hide "
             "for all teams except dmt/md/cto'). The 'Team' option only ever meant "
             "cross-team routing, which _mz_user_can_use_assign_radio already restricts "
             "to DMT/CTO/Admin/MD - a regular team member's radio was already readonly-"
             "locked to Self either way, but 'Team' (and 'Internal', neither ever usable "
             "for them) still showed as a visible-but-disabled option, which just looked "
             "like a broken/pointless control. Drives x_assign_type_no_team being shown "
             "instead of the real x_assign_type."
    )
    x_assign_type_no_team = fields.Selection(
        [('self', 'Self'), ('internal', 'Internal')],
        string="Assign Type", compute='_compute_x_assign_type_no_team',
        inverse='_inverse_x_assign_type_no_team',
        help="Third mirror of x_assign_type (see x_assign_type_no_internal), shown INSTEAD "
             "of the real one for a regular team's Agent/ATL/TL/Manager - see "
             "x_hide_team_option. 'Internal' is genuinely usable here (2026-09-15 client "
             "instruction: an ATL/TL/Manager may pick it to assign within their OWN team -"
             " see can_assign_within_own_team in _compute_x_assignable_user_ids), NOT "
             "readonly-locked to Self the way an older version of this comment used to "
             "say - a since-removed x_can_assign_salesperson_direct field briefly let "
             "them edit Salesperson directly while the radio still showed 'Self' instead "
             "of requiring 'Internal' first, which is exactly backwards from what "
             "'Self' is supposed to mean (client bug report, 2026-09-21: 'when selecting "
             "self radio, salesperson able to change, that should not happen')."
    )

    @api.depends('x_assign_type')
    def _compute_x_assign_type_no_internal(self):
        for lead in self:
            lead.x_assign_type_no_internal = (
                lead.x_assign_type if lead.x_assign_type != 'internal' else 'self'
            )

    @api.depends('x_assign_type')
    def _compute_x_assign_type_internal_only(self):
        for lead in self:
            lead.x_assign_type_internal_only = 'internal'

    @api.depends('x_assign_type')
    def _compute_x_assign_type_no_team(self):
        for lead in self:
            lead.x_assign_type_no_team = (
                lead.x_assign_type if lead.x_assign_type != 'team' else 'self'
            )

    def _inverse_x_assign_type_no_team(self):
        """Guarded on x_hide_team_option too, same reasoning as
        _inverse_x_assign_type_internal_only's own guard on x_show_internal_only -
        this field's compute always returns a value regardless of whether it's
        actually the visible one, and the web client still dirty-tracks/saves
        editable computed fields that are merely invisible. Without this guard a
        DMT/CTO/Admin/MD lead (where x_assign_type is the real, visible field)
        would get silently stomped back through this mirror's own narrower
        Self/Internal-only view of the value."""
        for lead in self:
            if not lead.x_hide_team_option:
                continue
            if lead.x_assign_type != lead.x_assign_type_no_team:
                lead.x_assign_type = lead.x_assign_type_no_team

    def _inverse_x_assign_type_internal_only(self):
        """Guarded on x_show_internal_only too (2026-09-12 fix), not just the
        recursive-write guard shared with _inverse_x_assign_type_no_internal below:
        this field's compute ALWAYS returns 'internal' (it's a single-option
        selection - there's nothing else it COULD return), regardless of whether
        it's actually the visible one. The web client still dirty-tracks and saves
        editable computed fields that are merely invisible, not skipped - so
        without this guard, a CTO/Admin lead saved as Self or Team (where this
        field is hidden and x_assign_type_no_internal is the real one) got its
        x_assign_type silently stomped back to 'internal' by THIS field's own
        inverse firing anyway (hit live 2026-09-12: a CTO's brand-new Self-assigned
        lead saved with x_assign_type='internal' instead)."""
        for lead in self:
            if not lead.x_show_internal_only:
                continue
            if lead.x_assign_type != 'internal':
                lead.x_assign_type = 'internal'

    def _inverse_x_assign_type_no_internal(self):
        """Guarded to a genuine no-op when the value already matches (2026-09-11 fix):
        `lead.x_assign_type = ...` on a real record is a plain attribute assignment,
        but Odoo implements that as `lead.write({'x_assign_type': ...})` under the
        hood - a full RECURSIVE call back into this model's own write() override,
        mid-flight, while the OUTER write() that triggered this inverse is still
        running. The web client sends x_assign_type_no_internal alongside
        x_assign_type in the SAME vals whenever the real field changes (it's an
        invisible mirror, but still a dependent compute the client tracks as
        dirty), both set to the SAME target value - so by the time this inverse
        fires, x_assign_type has already been applied by the outer write and
        already equals x_assign_type_no_internal. Without this guard, the
        recursive write still fired anyway, re-running every access check with
        team_id/user_id ALREADY updated (uncommitted) by the outer write - so a
        legitimate team handoff (which moves team_id off DMT) made the recursive
        write's own _mz_can_edit_owned check fail, and that failure rolled back
        the ENTIRE transaction, silently undoing the outer write's real change too
        (hit live 2026-09-11: DMT Agent's team-transfer save always reverted with
        an AccessError, even though nothing was actually wrong with the transfer
        itself)."""
        for lead in self:
            if lead.x_assign_type != lead.x_assign_type_no_internal:
                lead.x_assign_type = lead.x_assign_type_no_internal
    x_can_use_assign_radio = fields.Boolean(
        compute='_compute_x_assignable_user_ids',
        string="Can Use Assign Radio",
        help="Whether the CURRENT user may interact with the Assign Type radio at "
             "all - DMT team membership, MD, or CTO/Admin (_mz_user_can_use_assign_radio). "
             "Everyone else gets it readonly, forced to 'Self'. Not stored - reflects "
             "whoever has the form open."
    )
    x_can_assign_beyond_self = fields.Boolean(
        compute='_compute_x_assignable_user_ids',
        string="Can Assign Beyond Self",
        help="Whether the CURRENT user (the one viewing/editing this lead right now) "
             "may use the 'Team' or 'Internal' assign types: DMT team membership, or "
             "ATL/TL/Manager tier AND x_can_use_assign_radio (so only CTO/Admin, in "
             "practice, among non-DMT tiered users - see x_can_use_assign_radio). Not "
             "stored and not a property of the lead itself - it reflects whoever has "
             "the form open."
    )
    x_assignable_user_ids = fields.Many2many(
        'res.users', compute='_compute_x_assignable_user_ids',
        string="Assignable Users",
        help="The users user_id may be hand-picked from, when the current user is "
             "allowed to assign beyond Self (see x_can_assign_beyond_self) - otherwise "
             "empty. For 'Team': the selected team's create_lead_id members. For "
             "'Internal': whoever's directly in a group ranked below the current "
             "user's own group, per the team's configured privileges "
             "(_mz_team_subordinate_group_users) - DMT is exempt from both "
             "restrictions and always gets the full team roster. Used as user_id's "
             "domain in the view; not stored, purely a UI helper. An onchange-returned "
             "domain isn't reliably honored by the web client for Many2one search, so "
             "the domain lives in the view via this computed field instead."
    )
    x_hide_salesperson = fields.Boolean(
        compute='_compute_x_assignable_user_ids',
        string="Hide Salesperson Field",
        help="CTO/Admin and MD only: True on a lead THEY ALREADY OWN (same condition "
             "as x_hide_internal_option, and for the same reason - 'Internal' is "
             "meant for delegating someone ELSE's lead, and picking a salesperson "
             "only means anything alongside that). Shown (and pickable) again the "
             "moment they're viewing anyone else's lead, or creating a brand-new one. "
             "Not stored - reflects whoever has the form open."
    )
    x_can_create_partner = fields.Boolean(
        compute='_compute_x_can_create_partner',
        string="Can Create Partner",
        help="Whether the CURRENT user (viewing/editing this lead right now) may create "
             "a new res.partner from this form - Technology's own restriction (M2 sheet: "
             "'Agents may only SELECT from the existing partner and customer list; "
             "creating one is restricted to Team Leads and Managers'). True for every "
             "other BU (no such rule there) and for Technology TL/Manager tier; False "
             "for a Technology Agent/ATL. Not stored - reflects whoever has the form "
             "open."
    )
    x_hide_create_new_opportunity = fields.Boolean(
        compute='_compute_x_hide_create_new_opportunity',
        string="Hide Create New Opportunity",
        help="True for DMT, CTO/Admin and MD (client instruction, 2026-09-13) - 'Create "
             "New Opportunity' spins off a new lead for the SAME customer, routed to "
             "another team, which only makes sense for someone actually working a "
             "specific team's pipeline day-to-day. DMT already routes leads via the "
             "Team/Internal assign-type radio instead; CTO/Admin/MD have no team of "
             "their own to spin off FROM in the first place. Not per-record - reflects "
             "whoever has the form open, same as x_hide_internal_option."
    )

    @api.depends_context('uid')
    def _compute_x_hide_create_new_opportunity(self):
        user = self.env.user
        hide = (
            self._mz_user_is_dmt(user)
            or user.has_group('mazenet_access_rights.group_mzr_cto_admin')
            or user.has_group('mazenet_access_rights.group_mzr_md')
        )
        for lead in self:
            lead.x_hide_create_new_opportunity = hide

    @api.depends('team_id')
    @api.depends_context('uid')
    def _compute_x_can_create_partner(self):
        tier, _chain = self._mz_user_tier_chain(self.env.user)
        for lead in self:
            if lead.team_id.x_bu_category != 'tech':
                lead.x_can_create_partner = True
            else:
                lead.x_can_create_partner = tier in ('tl', 'manager')

    def _mz_team_subordinate_group_users(self, team, user):
        """Direct members (group.user_ids, NOT the transitively-implied
        all_user_ids) of every group ranked below `user`'s own group within
        `team`'s configured privileges (crm.team.privelege_ids) - i.e. only users
        under the current user in that team's configured hierarchy. Checked
        privilege by privilege, since sequence only ranks groups WITHIN one
        privilege (a Corporate team's several sub-team privileges each restart
        their own numbering). Empty recordset if the team has no privileges
        configured, or `user` doesn't hold any of their groups."""
        if not team or not team.privelege_ids:
            return self.env['res.users']
        owner_privilege = None
        owner_group = None
        for privilege in team.privelege_ids:
            for group in privilege.group_ids.sorted('sequence', reverse=True):
                if user in group.user_ids:
                    owner_privilege = privilege
                    owner_group = group
                    break
            if owner_group:
                break
        if not owner_group:
            return self.env['res.users']
        lower_groups = owner_privilege.group_ids.filtered(
            lambda g: g.sequence < owner_group.sequence
        )
        return lower_groups.mapped('user_ids')

    def _mz_reports_to_users(self, user):
        """Fallback pool for assigning a salesperson WITHIN a team, independent of
        crm.team.privelege_ids - _mz_team_subordinate_group_users above relies on
        that being configured per team, and NO team in this deployment actually
        has any privilege configured yet (mazenet_crm_team_privilege_rel is empty
        for every single team, confirmed live) - so that method always silently
        returns an empty pool, no matter who's asking or which team.

        REPLACES the old _mz_team_tier_subordinate_users (removed 2026-09-21,
        client instruction: "im tl1... dropdown should show only my agents who
        are reporting to me" - a real bug report, not a feature request: the old
        method picked every team.member_ids user whose TIER ranked below the
        caller's, TEAM-WIDE - so Hunter's TL-1 and TL-2 (or any team's ATL-1 and
        ATL-2) each saw EVERY agent on the whole team in their Salesperson
        dropdown, not just their own actual reports, since tier alone can't tell
        two same-tier peers' subordinates apart (every Hunter agent, whether
        under ATL-1 or ATL-2, shares one flat group_mzr_hunter_agent group -
        there's no per-superior group at all). This walks res.users.x_reports_to_id
        instead - a real reporting-line field this module now populates (see
        _mz_backfill_x_reports_to_hierarchy) - giving the FULL subtree under
        `user` (direct reports plus every report-of-a-report, e.g. a TL sees
        their ATLs' agents too, not just the TL's own two direct agents), scoped
        to exactly one person's own chain of command. Only a genuine fallback -
        _mz_team_subordinate_group_users is tried first wherever both are used,
        so a team that DOES eventually get real privileges configured keeps
        using that finer-grained ranking instead."""
        Users = self.env['res.users']
        result = Users
        frontier = Users.search([('x_reports_to_id', '=', user.id)])
        while frontier:
            result |= frontier
            frontier = Users.search([('x_reports_to_id', 'in', frontier.ids)])
        return result

    @api.depends('team_id', 'x_assign_type', 'user_id')
    @api.depends_context('uid')
    def _compute_x_assignable_user_ids(self):
        """DMT is a special case: a DMT team member may assign to ANY of the team's
        users under both 'Team' and 'Internal' - no restriction, and no ATL/TL/
        Manager tier gate either (DMT membership itself is enough - the
        Agent-restricted-to-Self rule is entirely waived for DMT). See
        _mz_check_assign_type_allowed for the matching server-side backstop - it
        must waive the tier gate for DMT the same way, or a DMT Agent could pick
        'Team'/'Internal' here and then get rejected on save.

        Everyone else keeps the normal tier-gated behavior: 'Team' restricted to
        create_lead_id members; 'Internal' restricted to whoever's DIRECTLY in a
        group ranked below the current user's own group, per the team's
        configured privileges (_mz_team_subordinate_group_users) - not the whole
        team roster."""
        user = self.env.user
        user_is_dmt = self._mz_user_is_dmt(user)
        can_use_radio = self._mz_user_can_use_assign_radio(user)
        is_md = user.has_group('mazenet_access_rights.group_mzr_md')
        is_cto_admin = user.has_group('mazenet_access_rights.group_mzr_cto_admin')
        tier, _chain = self._mz_user_tier_chain(user)
        # is_cto_admin is checked explicitly here, NEVER through the tier chain
        # (2026-09-12): group_mzr_cto_admin's own implied_ids include every team's
        # Manager group, and has_group() (which _mz_user_tier_chain relies on)
        # resolves implied groups transitively even without a real membership row -
        # so tier would otherwise misread CTO/Admin as a genuine team Manager. MD
        # is excluded on purpose - MD is hard-restricted to Self-only leads
        # everywhere else in this module (create()'s own MD gate), so 'beyond self'
        # would never actually be usable for them regardless of this flag.
        can_beyond_self = can_use_radio and (user_is_dmt or is_cto_admin or (not is_md and tier in ('atl', 'tl', 'manager')))
        is_cto_or_md = is_md or is_cto_admin
        for lead in self:
            # A regular team's own ATL/TL/Manager may use 'Internal' within their OWN
            # team (2026-09-15, client instruction: "internal radio button should be
            # selecteable in order to assign salesperson inside their team, that is
            # only for managers, tl's and atl's") - partially reverses the 2026-09-08
            # "Team/Internal is DMT+CTO/Admin+MD only" rule, but ONLY for 'Internal'
            # and ONLY on a lead already on their own team (most commonly an unowned
            # lead just handed off from DMT). 'Team' itself (cross-team routing)
            # stays DMT/CTO/Admin/MD-only - x_hide_team_option below still hides it
            # for them regardless of this. Computed per-lead (depends on lead.team_id
            # matching the user's own team) - moved ahead of x_can_use_assign_radio/
            # x_can_assign_beyond_self below so both can fold it in. The
            # _mz_is_corp_manager_oversight_team OR covers Corporate BU Manager's
            # 5-team oversight (2026-09-22) - see that method's own docstring.
            can_assign_within_own_team = bool(
                tier in ('atl', 'tl', 'manager') and lead.team_id
                and (
                    self._mz_user_own_team(user) == lead.team_id
                    or self._mz_is_corp_manager_oversight_team(user, lead.team_id)
                )
            )
            lead.x_can_use_assign_radio = can_use_radio or can_assign_within_own_team
            lead.x_can_assign_beyond_self = can_beyond_self or can_assign_within_own_team
            # CTO/Admin viewing an EXISTING lead owned by someone else (typically on
            # another team): the only sensible action is 'Internal' - reassigning
            # within that lead's own team hierarchy. Self/Team make no sense on a
            # lead that isn't theirs. Reworked 2026-09-12 (client correction, same
            # day as the first pass): a brand-new lead, or their own already-saved
            # one, ALWAYS gets Self/Team only (x_assign_type_no_internal) - Internal
            # is exclusively for someone else's existing lead, never their own,
            # including at creation time (the opposite of what the first pass did).
            is_other_owned_lead = bool(lead.id and lead.user_id and lead.user_id != user)
            show_internal_only = bool(is_cto_admin and is_other_owned_lead)
            hide_for_own_lead = bool(is_cto_or_md) and not show_internal_only
            # DMT hands a lead to the TEAM only when 'Team' is picked - the team lead
            # assigns the actual salesperson afterwards, so DMT never needs (or should
            # see) this field for that one combination (client instruction, 2026-09-09).
            # Mirrors assign_salesperson's onchange, which likewise leaves user_id unset
            # for DMT+'team' instead of auto-picking create_lead_id. CTO/Admin get the
            # exact same treatment for 'Team' now (2026-09-12): the salesperson is left
            # for the receiving team's own TL to pick, not CTO/Admin.
            hide_for_dmt_team = bool((user_is_dmt or is_cto_admin) and lead.x_assign_type == 'team')
            lead.x_hide_salesperson = hide_for_own_lead or hide_for_dmt_team
            lead.x_hide_internal_option = hide_for_own_lead
            lead.x_show_internal_only = show_internal_only
            lead.x_hide_team_option = not is_cto_or_md and not user_is_dmt
            if not can_beyond_self and not can_assign_within_own_team:
                lead.x_assignable_user_ids = False
            elif user_is_dmt or (is_cto_admin and show_internal_only):
                # CTO/Admin's 'internal' pool would otherwise be empty:
                # _mz_team_subordinate_group_users checks raw, non-transitive group
                # membership, and CTO/Admin never actually gets a real row in any
                # team's own privilege groups (only the transitive has_group() result
                # used above, which doesn't apply here) - give them the same
                # unrestricted whole-team-roster pool as DMT for this one path.
                lead.x_assignable_user_ids = lead.team_id.member_ids
            elif can_use_radio and lead.x_assign_type == 'team':
                # 'Team' pool (create_lead_id) only applies to whoever can actually
                # DRIVE the radio (DMT/CTO/Admin/MD) - a regular team's own ATL/TL/
                # Manager reassigning an already-team-owned lead's salesperson always
                # uses the subordinate-hierarchy pool below instead, regardless of the
                # record's residual stored x_assign_type (e.g. still 'team' from
                # whoever routed it here in the first place).
                lead.x_assignable_user_ids = lead.team_id.create_lead_id
            else:
                pool = self._mz_team_subordinate_group_users(lead.team_id, user)
                lead.x_assignable_user_ids = pool if pool else self._mz_reports_to_users(user)

    @api.onchange('x_assign_type', 'team_id')
    def assign_salesperson(self):
        """x_assign_type drives how user_id gets populated - see the field's help.
        'Team' and 'Internal' both leave user_id hand-pickable, restricted to
        x_assignable_user_ids (create_lead_id members for 'Team', full team roster
        for 'Internal') - create_lead_id is a Many2many now, so there's no longer a
        single value to auto-assign for 'Team'."""
        user = self.env.user
        if self.x_assign_type == 'self':
            self.user_id = self.env.user
            self.team_id = self._mz_user_own_team()
            return
        if self.x_assign_type == 'team':
            # DMT only ever hands a lead to the TEAM, never to a specific person - the
            # team lead picks the actual salesperson afterwards (client instruction,
            # 2026-09-09). Leave user_id unset rather than auto-picking create_lead_id;
            # the Salesperson field is hidden for this exact combination in the view
            # (x_hide_salesperson) so there's nothing for the DMT user to fill in anyway.
            # CTO/Admin get the same treatment (2026-09-12): 'Team' only ever appears on
            # their OWN lead (never someone else's - see x_show_internal_only), so this
            # is always a hand-off to whichever team is picked, salesperson TBD by that
            # team's own TL.
            if self._mz_user_is_dmt(user) or user.has_group('mazenet_access_rights.group_mzr_cto_admin'):
                self.user_id = False
                # Keep Stage 3's own x_target_team_id in sync when the transfer
                # happens via the radio instead of that field directly (2026-09-13)
                # - x_target_team_id IS the official transfer now (see its own
                # onchange below), so whichever side the user actually used, both
                # should end up agreeing - otherwise the Transfer Completeness Gate
                # would reject the save for a field that's arguably already answered.
                if self._mz_user_is_dmt(user):
                    self.x_target_team_id = self.team_id
                return
            self.user_id = self.team_id.create_lead_id and self.team_id.create_lead_id[0] or False
        if self.x_assign_type == 'internal':
            # Only snap team_id to the ACTING user's own team when they have one - a
            # regular TL/Manager picking 'Internal' is always delegating within their
            # own team, so this is the normal case. CTO/Admin (and MD) have no
            # crm_team_ids of their own at all - user.crm_team_ids[0] on an empty
            # recordset raised IndexError the moment they picked 'Internal' on
            # someone ELSE's lead (2026-09-08). For them, team_id already correctly
            # holds whatever team the lead they're viewing belongs to - leave it as
            # is instead of forcing a team that doesn't exist for this user.
            # Corporate BU Manager (2026-09-22) needs the SAME "leave it as is"
            # treatment for their own 5 oversight teams specifically - their real
            # crm_team_ids/own_team is team_corporate, which would otherwise
            # WRONGLY snap e.g. a Hunter lead's team_id away to Corporate the
            # instant they picked 'Internal' (see _mz_is_corp_manager_oversight_team).
            if self._mz_is_corp_manager_oversight_team(user, self.team_id):
                return
            if user.crm_team_ids:
                self.team_id = user.crm_team_ids[0]
            return
        if not self.x_can_assign_beyond_self:
            self.x_assign_type = 'self'
            self.user_id = user
            return {'warning': {
                'title': _("Assignment restricted"),
                'message': _("Only DMT team members and CTO/Admin can assign to a team "
                            "or assign internally. Everyone else can only assign to "
                            "themselves."),
            }}
        if self.user_id not in self.x_assignable_user_ids:
            self.user_id = False

    @api.onchange('x_target_team_id')
    def _onchange_x_target_team_id(self):
        """Stage 3 (Transfer to BU)'s own x_target_team_id IS the official transfer
        now (client instruction, 2026-09-13) - picking a team there sets the REAL
        team_id/x_assign_type the exact same way the Team radio would, instead of
        being a separate, purely informational field that then needed a SECOND,
        redundant action via the radio to actually route the lead anywhere. Setting
        x_assign_type here cascades into assign_salesperson's own 'team' branch
        (standard Odoo onchange chaining), which clears user_id and syncs
        x_target_team_id back from team_id - a no-op in that direction since
        they're already equal at that point. Only meaningful while still on DMT -
        x_target_team_id only appears in the view then anyway (x_team_bu_category)."""
        if self.x_target_team_id and self._mz_user_is_dmt(self.env.user):
            self.team_id = self.x_target_team_id
            self.x_assign_type = 'team'

    @api.depends(
        'activity_ids.date_deadline', 'activity_ids.calendar_event_id.start',
        'activity_ids.mz_activity_time', 'activity_ids.user_id', 'activity_ids.active',
    )
    def _compute_x_next_activity_datetime(self):
        for lead in self:
            candidates = []
            for activity in lead.activity_ids.filtered('active'):
                candidates.append(activity._mz_resolve_activity_datetime())
            candidates = [c for c in candidates if c]
            lead.x_next_activity_datetime = min(candidates) if candidates else False

    x_is_locked = fields.Boolean(
        string="RED Lock Active",
        default=False,
        help="Indicates if lead is currently locked due to RED timer expiration."
    )

    x_lock_date = fields.Datetime(
        string="RED Lock Date",
        help="Timestamp when RED lock was triggered."
    )

    x_content_readonly_for_me = fields.Boolean(
        compute="_compute_x_content_readonly_for_me",
        string="Read-Only For Me",
        help="Whether write() would actually reject a content edit from the CURRENT "
             "user right now - covers both RED-lock read-only AND the team-transfer "
             "rule: once a lead's team_id moves off wherever gave someone access "
             "(_mz_can_edit_owned/_mz_can_edit_by_team), it goes read-only for them, "
             "locked or not - there is NO owner exemption while unlocked either, "
             "Sales Team (team_id) is the single source of truth for both the "
             "transfer action and this check, whether the lead is owned or not (an "
             "unowned lead just always fails the 'am I the owner' half of "
             "_mz_can_edit_owned, so it needs ATL/TL/Manager tier same as a non-owner "
             "editing someone else's lead). CTO/Admin bypass everything. While LOCKED "
             "specifically, the owner is excluded even on their own team - being "
             "locked out is the whole point of RED lock for them. Not stored - it "
             "reflects whoever has the form open, same pattern as "
             "x_can_assign_beyond_self."
    )

    @api.depends('x_is_locked', 'team_id', 'user_id')
    @api.depends_context('uid')
    def _compute_x_content_readonly_for_me(self):
        u = self.env.user
        is_cto_admin = u.has_group("mazenet_access_rights.group_mzr_cto_admin")
        for lead in self:
            if self.env.su or is_cto_admin:
                lead.x_content_readonly_for_me = False
            elif lead.x_is_locked:
                lead.x_content_readonly_for_me = not lead._mz_can_edit_by_team(u)
            else:
                lead.x_content_readonly_for_me = not lead._mz_can_edit_owned(u)

    x_team_transfer_readonly = fields.Boolean(
        compute="_compute_x_team_transfer_readonly",
        string="Read-Only (Team Transfer)",
        help="True specifically when this lead is read-only for the CURRENT user "
             "because team_id no longer includes them (a genuine transfer to another "
             "team) - NOT because of a RED lock, and NOT because of the separate "
             "'need ATL/TL/Manager tier to edit a peer's owned lead' rule that applies "
             "WITHIN a team you're still a member of (_mz_can_edit_by_team). Kept "
             "separate from x_content_readonly_for_me (which covers all three reasons)"
             " so the UI can label each cause correctly: RED lock already gets red "
             "(ribbon/tint/banner) elsewhere, an actual team transfer gets this grey "
             "tint/'TRANSFERRED' banner, and the peer-lead-tier case just goes plain "
             "readonly with no banner at all - conflating that last one with "
             "'TRANSFERRED' was actively misleading (the lead never left the team;"
             " hit live 2026-09-11 once DMT stopped being exempt from the tier rule "
             "and this mislabeling became visible for the first time). Not stored - "
             "same per-user reasoning as x_content_readonly_for_me."
    )

    @api.depends('x_is_locked', 'x_content_readonly_for_me', 'team_id')
    @api.depends_context('uid')
    def _compute_x_team_transfer_readonly(self):
        u = self.env.user
        for lead in self:
            lead.x_team_transfer_readonly = (
                lead.x_content_readonly_for_me
                and not lead.x_is_locked
                and (not lead.team_id or u not in lead.team_id.member_ids)
            )

    x_activity_card_state = fields.Selection(
        [
            ('green', 'Activity Scheduled'),
            ('yellow', 'Activity Due Soon'),
            ('orange', 'Activity Due Very Soon'),
            ('red', 'Activity Overdue (RED Lock)'),
        ],
        string="Activity Card State", compute="_compute_x_activity_card_state", store=True,
        help="Drives the Pipeline kanban card's colour, for DMT/Tally/Technology leads only "
             "(MZ_ACTIVITY_CARD_BU_CATEGORIES - Software Dev and MIS have no Follow-up's "
             "stage, so this doesn't apply to them; False/'Normal' for every other lead "
             "regardless of team). Client instruction (2026-09-21), a 4-stage countdown to "
             "x_next_activity_datetime, precedence top to bottom:\n"
             "- RED: same signal as x_is_locked (the RED lock) - " + str(MZ_ACTIVITY_WINDOW_MINUTES) + " minutes "
             "past the activity's real moment with it still open. Deliberately reuses "
             "x_is_locked rather than its own independent timer, so there's exactly one "
             "'is this overdue' answer in the whole module.\n"
             "- ORANGE: from " + str(MZ_ACTIVITY_ORANGE_MINUTES) + " minutes before the activity, "
             "through the due moment itself, up to the RED lock above (no separate colour for "
             "'just passed due time, not yet locked' - orange covers that gap too).\n"
             "- YELLOW: from " + str(MZ_ACTIVITY_YELLOW_MINUTES) + " down to " + str(MZ_ACTIVITY_ORANGE_MINUTES) + " "
             "minutes before the activity.\n"
             "- GREEN: baseline whenever there's a scheduled activity at all, further out "
             "than " + str(MZ_ACTIVITY_YELLOW_MINUTES) + " minutes - ANY future activity, not "
             "restricted to 'today' (replaces the earlier purple 'activity today' state).\n"
             "A plain Selection, NOT the kanban 'color' integer (that's a colour-picker "
             "index, unrelated to this). Stored, because it needs to be orderable/filterable "
             "and - critically - a card must repaint purely because TIME has passed even "
             "when nothing on the record was written, which a stored field can only do via "
             "an explicit periodic recompute: ir_cron_mz_recompute_activity_card_state "
             "(data/cron.xml) re-triggers this compute, every 5 minutes, for leads whose "
             "activity falls within a day of now (a cheap, indexed window - not a full-table "
             "sweep) - ordinary field writes (a new/edited/completed activity, a fresh RED "
             "lock) still recompute it immediately via these @api.depends as usual."
    )

    @api.depends('x_next_activity_datetime', 'x_is_locked', 'team_id.x_bu_category')
    def _compute_x_activity_card_state(self):
        now = fields.Datetime.now()
        for lead in self:
            if lead.team_id.x_bu_category not in MZ_ACTIVITY_CARD_BU_CATEGORIES:
                lead.x_activity_card_state = False
                continue
            if lead.x_is_locked:
                lead.x_activity_card_state = 'red'
                continue
            if not lead.x_next_activity_datetime:
                lead.x_activity_card_state = False
                continue
            minutes_until = (lead.x_next_activity_datetime - now).total_seconds() / 60
            if minutes_until <= MZ_ACTIVITY_ORANGE_MINUTES:
                lead.x_activity_card_state = 'orange'
            elif minutes_until <= MZ_ACTIVITY_YELLOW_MINUTES:
                lead.x_activity_card_state = 'yellow'
            else:
                lead.x_activity_card_state = 'green'

    @api.model
    def _cron_recompute_activity_card_state(self):
        """Forces x_activity_card_state (a stored field) to repaint purely because time has
        passed - see that field's own help text for why a cron is needed at all. Scoped to
        DMT/Tally/Technology leads whose activity falls within a day of now: wide enough to
        safely cover every timezone's 'today' without a per-row timezone calculation in SQL
        (the exact per-user-timezone check happens in the compute itself), but nowhere close
        to a full-table sweep - x_next_activity_datetime is indexed, and this excludes every
        lead with a far-future/past/no activity, or in a BU this feature doesn't apply to."""
        now = fields.Datetime.now()
        leads = self.sudo().search([
            ('x_next_activity_datetime', '>=', now - timedelta(days=1)),
            ('x_next_activity_datetime', '<=', now + timedelta(days=1)),
            ('team_id.x_bu_category', 'in', list(MZ_ACTIVITY_CARD_BU_CATEGORIES)),
            ('active', '=', True),
        ])
        if leads:
            leads._compute_x_activity_card_state()

    # ------------------------------------------------------------------
    # M2 pipeline fields (Mazenet_CRM_M2_Build_Tasks.xlsx)
    # Shared across two or more of the 5 BU pipelines - same concept, one
    # field, gated per-team in the view via x_team_bu_category.
    # ------------------------------------------------------------------
    x_team_bu_category = fields.Selection(
        related='team_id.x_bu_category', string="Team BU Category",
        help="Plain (non-dotted) mirror of team_id.x_bu_category for use in the view's "
             "invisible attrs - a dotted 'team_id.x_bu_category' expression isn't reliably "
             "fetched by the web client since x_bu_category otherwise never appears "
             "anywhere in this view's own field spec, which left every Pipeline Fields "
             "group permanently invisible."
    )
    x_current_stage_gate_fields = fields.Char(
        compute='_compute_x_current_stage_gate_fields',
        string="Current Stage Required Fields",
        help="Comma-delimited (leading/trailing commas included, so 'in' checks in the "
             "view can match a whole field name and not a substring of a longer one - "
             "e.g. 'x_feasibility' vs 'x_feasibility_identified') list of the field "
             "names MZ_STAGE_GATE_RULES requires to move OUT of the lead's CURRENT "
             "stage - drives the red-asterisk 'required' indicator on those fields in "
             "the Pipeline Fields page (view can't itself resolve MZ_STAGE_GATE_RULES "
             "or match against team_id.x_bu_category via a dotted expression, so this "
             "compute does it server-side). Purely a UI indicator matching what "
             "_mz_stage_gate_check will actually enforce on the next forward stage "
             "move - NOT a stored/model-level required=True, so it doesn't block "
             "saving while just sitting on the current stage, only shows the marker. "
             "Not stored - reflects the record's own state, recomputed on stage_id/"
             "team_id change."
    )

    @api.depends('stage_id', 'team_id')
    def _compute_x_current_stage_gate_fields(self):
        for lead in self:
            team = lead.team_id
            resolved = lead._mz_stage_gate_rules_resolved(team.x_bu_category) if team else []
            field_names = next(
                (names for stage, names in resolved if stage.id == lead.stage_id.id), []
            )
            lead.x_current_stage_gate_fields = (
                ',' + ','.join(field_names) + ',' if field_names else False
            )

    x_product_service = fields.Char(string="Product / Service")
    x_feasibility = fields.Char(string="Feasibility")
    x_timeline = fields.Char(string="Timeline")
    x_requirements_attachment_ids = fields.Many2many(
        'ir.attachment', 'mazenet_crm_lead_requirements_attachment_rel',
        'lead_id', 'attachment_id', string="Requirements Attachment(s)")
    x_quote_date = fields.Date(string="Quote Date")
    x_quote_document_ids = fields.Many2many(
        'ir.attachment', 'mazenet_crm_lead_quote_document_rel',
        'lead_id', 'attachment_id', string="Quote Document(s)")
    x_company_turnover = fields.Monetary(string="Company Turnover", currency_field='company_currency')
    x_project_start_date = fields.Date(string="Project Start Date")
    x_project_start_attachment_ids = fields.Many2many(
        'ir.attachment', 'mazenet_crm_lead_project_start_attachment_rel',
        'lead_id', 'attachment_id', string="Project Start Attachment(s)")
    x_project_completed_date = fields.Date(string="Project End Date")
    x_project_completed_attachment_ids = fields.Many2many(
        'ir.attachment', 'mazenet_crm_lead_project_completed_attachment_rel',
        'lead_id', 'attachment_id', string="Project Completed Attachment(s)")
    x_workorder_completion_date = fields.Date(
        string="Project Actual End Date",
        help="MANUAL entry only - do not build an auto-fetch or any integration with the "
             "Work Order app."
    )
    x_deviation_days = fields.Integer(
        string="Deviation Days", compute="_compute_x_deviation_days", store=True,
        help="Auto-calculated from Project End Date vs Project Actual End Date."
    )
    x_demo_completed = fields.Boolean(string="Demo / POC Completed")
    x_system_study_attachment_ids = fields.Many2many(
        'ir.attachment', 'mazenet_crm_lead_system_study_attachment_rel',
        'lead_id', 'attachment_id', string="System Study (PDF)")

    @api.depends('x_project_completed_date', 'x_workorder_completion_date')
    def _compute_x_deviation_days(self):
        for lead in self:
            if lead.x_project_completed_date and lead.x_workorder_completion_date:
                lead.x_deviation_days = (
                    lead.x_workorder_completion_date - lead.x_project_completed_date
                ).days
            else:
                lead.x_deviation_days = 0

    @api.constrains('phone', 'email_from')
    def _mz_check_contact_format(self):
        """Build-notes: 'Format validation only... Show a placeholder hint. No
        dummy-number/dummy-email detection.' - just syntax, not a mandatory-field or
        real-number/real-address check. Scoped to the 5 M2 BUs
        (MZ_FORMAT_VALIDATED_BU_CATEGORIES); empty values are fine here (mandatory-ness
        is the stage gate's job, see MZ_STAGE_GATE_RULES) - this only fires once
        something has actually been typed in."""
        for lead in self:
            if lead.team_id.x_bu_category not in MZ_FORMAT_VALIDATED_BU_CATEGORIES:
                continue
            # 10-digit phone format check removed for now (client instruction,
            # 2026-09-16) - the phone widget's own auto-formatting (removed
            # 2026-09-15, see the "Contact Number" field's own view comment) was
            # only ONE way a number could end up not matching this pattern; rather
            # than keep chasing every input path that could produce a differently-
            # formatted-but-legitimate number, the format check itself is dropped.
            # Email format validation is unaffected, still enforced below.
            if lead.email_from and not MZ_EMAIL_RE.fullmatch(lead.email_from.strip()):
                raise ValidationError(_(
                    "'%(lead)s': Email must be a valid email address (got '%(value)s')."
                ) % {'lead': lead.name, 'value': lead.email_from})

    # -- DMT only --
    x_organic_inorganic = fields.Selection(
        [('organic', 'Organic'), ('inorganic', 'In-Organic')],
        string="Organic / In-Organic", help="DMT only. Do not use on any other BU."
    )
    x_company_or_individual = fields.Char(string="Company / Individual")
    x_contact_purpose = fields.Char(string="Contact Purpose")
    x_employee_count = fields.Integer(string="Employee Count")
    x_target_team_id = fields.Many2one(
        'crm.team', string="Target Business Unit",
        help="The BU this DMT lead is being transferred to. Excludes the lead's own "
             "current team - see team_id's own comment for why that domain lives in "
             "the view (crm_lead_views.xml) instead of here."
    )
    x_transfer_notes = fields.Text(string="Transfer Notes / Reason")

    # -- DMT Details snapshot (client instruction, 2026-09-13) --
    # Frozen copies of DMT's own Pipeline Fields tab, captured ONCE at the moment a
    # DMT-originated lead's team_id first moves off DMT (write()'s "DMT Details
    # Snapshot" block below) - NOT because the live fields above lose their data (a
    # view's invisible="x_team_bu_category != 'dmt'" only hides them, never erases
    # anything), but because two of them (source_id, x_product_service) are genuinely
    # SHARED with the receiving team's own M2/M3 stage-gate fields and get
    # overwritten for real once that team starts working the lead - without a
    # separate copy, DMT's own original answer there would be gone for good. Shown
    # in a dedicated, always-read-only "DMT Details" tab (views/crm_lead_views.xml)
    # next to Pipeline Fields, visible once x_dmt_snap_captured is set - i.e. once
    # there's actually something to show.
    x_dmt_snap_captured = fields.Boolean(copy=False, help="Guards the one-time snapshot write below against firing again on a LATER handoff between two other teams.")
    x_dmt_snap_organic_inorganic = fields.Selection(
        [('organic', 'Organic'), ('inorganic', 'In-Organic')], string="Organic / In-Organic (at handoff)"
    )
    x_dmt_snap_source_id = fields.Many2one('utm.source', string="Source (at handoff)")
    x_dmt_snap_referred = fields.Char(string="Source Reference (at handoff)")
    x_dmt_snap_company_or_individual = fields.Char(string="Company / Individual (at handoff)")
    x_dmt_snap_contact_purpose = fields.Char(string="Contact Purpose (at handoff)")
    x_dmt_snap_product_service = fields.Char(string="Product / Service (at handoff)")
    x_dmt_snap_employee_count = fields.Integer(string="Employee Count (at handoff)")
    x_dmt_snap_company_turnover = fields.Monetary(
        string="Company Turnover (at handoff)", currency_field='company_currency'
    )
    x_dmt_snap_target_team_id = fields.Many2one('crm.team', string="Target Business Unit (at handoff)")
    x_dmt_snap_transfer_notes = fields.Text(string="Transfer Notes / Reason (at handoff)")

    # -- Tally only --
    x_tally_category = fields.Selection(
        [
            ('tdl', 'TDL'),
            ('tally_licence', 'Tally Licence'),
            ('tally_cloud', 'Tally Cloud (Mazenet / AWS / Oracle)'),
            ('tally_amc', 'Tally AMC (Online / Direct)'),
            ('maze_chit', 'Maze Chit'),
            ('mobile_app', 'Mobile App'),
            ('renewal', 'Renewal'),
            ('software_development', 'Software Development'),
            ('not_tally_or_chit', 'Not Tally or Chit Related'),
        ],
        string="Lead Category"
    )
    x_company_intro_done = fields.Text(string="Company Intro")

    # -- Technology only --
    x_customer_status = fields.Char(string="Customer Status")
    x_customer_need = fields.Char(string="Customer Need")
    x_feasibility_identified = fields.Boolean(string="Feasibility Evaluation Identified")
    x_bom_attachment_ids = fields.Many2many(
        'ir.attachment', 'mazenet_crm_lead_bom_attachment_rel',
        'lead_id', 'attachment_id', string="BOM Received")
    x_boq_attachment_ids = fields.Many2many(
        'ir.attachment', 'mazenet_crm_lead_boq_attachment_rel',
        'lead_id', 'attachment_id', string="BOQ Received")
    x_quote_shared_checkbox = fields.Boolean(string="Shared with Customer")
    x_customer_goods_finalised = fields.Boolean(string="Customer Goods Finalised")

    # -- Software Dev only --
    x_branch_count = fields.Char(string="No. of Branches")
    x_sw_employee_count = fields.Char(string="No. of Employees")
    x_nature_of_business = fields.Char(string="Nature of Business")
    x_established_year = fields.Char(string="Established Year")
    x_founder = fields.Char(string="Founder")
    x_ceo = fields.Char(string="CEO")
    x_meeting_attendees = fields.Char(string="Client Details (Meeting Attendees)")

    # -- MIS only --
    x_target_audience = fields.Char(string="Target Audience")
    x_mis_timelines_estimate = fields.Integer(string="Timelines (Estimate)")
    x_deliverables = fields.Char(string="Deliverables")

    # -- Corporate Pipeline (Hunter / Account Manager / Corp Training Delivery) --
    # Mazenet_CRM_Corporate_LMS_TNH_Pipelines.xlsx. Named generically (no "corp_"
    # prefix) wherever the same concept is expected to recur on LMS's own Stage 8
    # "Training Status" (Training Commenced/Completed) when that pipeline is built -
    # x_target_audience/x_deliverables above are already shared the same way.
    # Stage 1 (New Lead/Source) and Stage 4 (Quote shared) need no new fields at all:
    # source_id/referred (stock, via utm.source.x_requires_reference_text) and
    # x_quote_document_ids (Tally/Tech/SWDev/MIS's own field) are reused as-is.
    x_client_expectations_attachment_ids = fields.Many2many(
        'ir.attachment', 'mazenet_crm_lead_client_expectations_attachment_rel',
        'lead_id', 'attachment_id', string="Client Expectations Attachment(s)")
    x_lead_timelines_days = fields.Integer(
        string="Timelines",
        help="Number box, per the Corporate/LMS sheets - NOT the same field as the "
             "generic Char x_timeline used by Tally/Technology/Software Dev's own "
             "Stage 3/4 'Timeline' item."
    )
    x_presentation_completed_datetime = fields.Datetime(
        string="Presentation Completed",
        help="Date AND time, not date only - the sheet is explicit that a bare date isn't enough."
    )
    x_training_content_eval_start_date = fields.Date(string="Training Content Evaluation Started")
    x_training_content_preapproved = fields.Boolean(
        string="Pre-Approved",
        help="When checked, Training Content Finalized is NOT required to advance past Evaluation."
    )
    x_training_content_finalized_date = fields.Date(
        string="Training Content Finalized",
        help="Mandatory unless Pre-Approved is checked - see MZ_WAIVER_CONDITIONAL_MANDATORY_FIELDS."
    )
    x_trainer_eval_start_date = fields.Date(string="Trainer Evaluation Started")
    x_trainer_mazenet_validated = fields.Boolean(
        string="Mazenet Validation",
        help="When checked, Trainer Evaluation Finalized is NOT required to advance past Evaluation."
    )
    x_trainer_eval_finalized_date = fields.Date(
        string="Trainer Evaluation Finalized",
        help="Mandatory unless Mazenet Validation is checked - see MZ_WAIVER_CONDITIONAL_MANDATORY_FIELDS."
    )
    x_training_dates_finalized = fields.Date(string="Training Dates Finalized")
    x_po_received_date = fields.Date(string="PO Received Date (Vendor / Client)")
    x_po_received_attachment_ids = fields.Many2many(
        'ir.attachment', 'mazenet_crm_lead_po_received_attachment_rel',
        'lead_id', 'attachment_id', string="PO Received (Vendor / Client)",
        help="Existing clients: 1-year PO or SOW. New clients: PO, SOW or mail confirmation."
    )
    x_po_issued_trainer_date = fields.Date(string="PO Issued to Trainer Date")
    x_po_issued_trainer_attachment_ids = fields.Many2many(
        'ir.attachment', 'mazenet_crm_lead_po_issued_trainer_attachment_rel',
        'lead_id', 'attachment_id', string="PO Issued to Trainer",
        help="A different document from PO Received above - the Won gate checks both by document type."
    )
    x_training_commenced_date = fields.Date(string="Training Commenced")
    x_training_commenced_attachment_ids = fields.Many2many(
        'ir.attachment', 'mazenet_crm_lead_training_commenced_attachment_rel',
        'lead_id', 'attachment_id', string="Training Commenced Attachment(s)")
    x_training_completed_date = fields.Date(string="Training Completed")
    x_training_completed_attachment_ids = fields.Many2many(
        'ir.attachment', 'mazenet_crm_lead_training_completed_attachment_rel',
        'lead_id', 'attachment_id', string="Training Completed Attachment(s)")

    # -- LMS Pipeline (team_lms) only --
    # Mazenet_CRM_Corporate_LMS_TNH_Pipelines.xlsx. Stage 1/2/3/4 reuse Corporate's own
    # fields above (x_client_expectations_attachment_ids, x_product_service,
    # x_target_audience, x_lead_timelines_days, x_deliverables,
    # x_presentation_completed_datetime, x_quote_document_ids) - the sheets list
    # identical field shapes there. Stage 5 (Won/Lost) uses stock's default Lost
    # functionality (no custom field - see action_set_lost).
    # Stage 8 ("Training Status", folded into Project State) reuses
    # x_training_commenced/completed_* above plus the standard 4 Project State fields.
    x_lms_training_content = fields.Selection(
        [('pre_approved', 'Pre-approved'), ('new_content', 'New Content')],
        string="Training Content"
    )
    x_lms_toc_by = fields.Selection(
        [('trainer', 'Trainer'), ('mazenet', 'Mazenet')],
        string="New Content - TOC By",
        help="Only applies when Training Content = New Content - mandatory in that case only, "
             "see MZ_SELECTION_CONDITIONAL_MANDATORY_FIELDS."
    )
    x_lms_content_availability = fields.Selection(
        [('all', 'All'), ('week_by_week', 'Week-by-Week')],
        string="Content Available"
    )
    x_lms_training_start_date = fields.Date(string="Training Duration - Start")
    x_lms_training_end_date = fields.Date(string="Training Duration - End")
    x_lms_training_weeks = fields.Integer(
        string="Training Duration in Weeks", compute="_compute_x_lms_training_weeks", store=True,
        help="Auto-calculated from the Training Duration date range: each started 7-day "
             "block counts as one week (0-6 days = 1 week, 7-13 days = 2 weeks, etc). "
             "Read-only - drives how many rows _mz_sync_lms_weeks generates below."
    )
    x_lms_week_ids = fields.One2many(
        'crm.lead.lms.week', 'lead_id', string="Weekly KT / Skill Upload Tracking"
    )

    capture_dmt_lead_id = fields.Many2one(
        'res.users', string='DMT Person (Transferred By)', ondelete='set null', copy=False,
        help="The DMT user who transferred this lead to another team. Captured "
             "automatically, once, the instant a DMT-owned lead's team_id first "
             "moves off DMT - see write()'s DMT Details Snapshot block."
    )

    @api.depends('x_lms_training_start_date', 'x_lms_training_end_date')
    def _compute_x_lms_training_weeks(self):
        for lead in self:
            if lead.x_lms_training_start_date and lead.x_lms_training_end_date \
                    and lead.x_lms_training_end_date >= lead.x_lms_training_start_date:
                days = (lead.x_lms_training_end_date - lead.x_lms_training_start_date).days
                lead.x_lms_training_weeks = days // 7 + 1
            else:
                lead.x_lms_training_weeks = 0

    def _mz_sync_lms_weeks(self):
        """Reconciles x_lms_week_ids against x_lms_training_weeks (Build Notes #5: "one
        row per week, generated from the Stage 6 date range - DO NOT build fixed
        checkbox fields"). Adds missing week rows and removes rows past the new count,
        but never touches an existing week's own kt_uploaded/skill_uploaded flags - a
        lead whose training got extended from 5 to 7 weeks should keep what was already
        ticked on weeks 1-5, not have them reset."""
        Week = self.env['crm.lead.lms.week']
        for lead in self:
            target = lead.x_lms_training_weeks
            existing = Week.search([('lead_id', '=', lead.id)])
            existing_numbers = set(existing.mapped('week_number'))
            to_remove = existing.filtered(lambda w: w.week_number > target)
            if to_remove:
                to_remove.unlink()
            missing = [n for n in range(1, target + 1) if n not in existing_numbers]
            if missing:
                Week.create([{'lead_id': lead.id, 'week_number': n} for n in missing])

    # -- TNH Pipeline (team_tnh) only --
    # Mazenet_CRM_Corporate_LMS_TNH_Pipelines.xlsx. Stage 1 reuses source_id/referred
    # like every other BU. Stage 2 reuses x_company_turnover ("Revenue" here - same
    # Monetary field, relabeled in the view), x_employee_count (DMT's own field) and
    # x_nature_of_business (Software Dev's own field). Stage 4 reuses
    # x_presentation_completed_datetime. Won/Lost uses stock's default Lost
    # functionality (no custom field) and x_quote_document_ids (the "tagged
    # Quotation or Proposal attachment" Stage 8's
    # Won gate asks for - the sheet's own Stage 5 "Proposal" field list is all
    # checkboxes with no attachment item of its own, so this is added onto that stage
    # rather than invented as a new field). Project State reuses the standard 4 fields
    # plus x_workorder_completion_date/x_deviation_days, same as every other BU.
    x_tnh_meeting_held = fields.Boolean(string="Meeting")
    x_tnh_meeting_attachment_ids = fields.Many2many(
        'ir.attachment', 'mazenet_crm_lead_tnh_meeting_attachment_rel',
        'lead_id', 'attachment_id', string="Meeting Attachment(s)")
    x_tnh_service_fte = fields.Boolean(string="Service - FTE")
    x_tnh_service_cwr = fields.Boolean(string="Service - CWR")
    x_tnh_service_iaas = fields.Boolean(string="Service - IaaS")
    x_tnh_service_htd = fields.Boolean(string="Service - HTD")
    x_tnh_proposal_fte = fields.Boolean(string="Proposal for - FTE")
    x_tnh_proposal_cwr = fields.Boolean(string="Proposal for - CWR")
    x_tnh_proposal_iaas = fields.Boolean(string="Proposal for - IaaS")
    x_tnh_proposal_htd = fields.Boolean(string="Proposal for - HTD")
    x_tnh_deviation = fields.Boolean(string="Deviation")
    x_tnh_deviation_text = fields.Text(
        string="Deviation Notes",
        help="Mandatory ONLY IF Deviation is checked - see MZ_BOOLEAN_CONDITIONAL_MANDATORY_FIELDS."
    )
    x_tnh_agreement_doc_ids = fields.Many2many(
        'ir.attachment', 'mazenet_crm_lead_tnh_agreement_attachment_rel',
        'lead_id', 'attachment_id', string="NDA / Confirmation Mail / Messenger Confirmation",
        help="Any one of the three (NDA, confirmation mail, messenger confirmation) satisfies "
             "this - upload whichever one actually applies."
    )
    x_tnh_msa_attachment_ids = fields.Many2many(
        'ir.attachment', 'mazenet_crm_lead_tnh_msa_attachment_rel',
        'lead_id', 'attachment_id', string="MSA", help="Optional - not every deal has one.")
    x_tnh_sow_attachment_ids = fields.Many2many(
        'ir.attachment', 'mazenet_crm_lead_tnh_sow_attachment_rel',
        'lead_id', 'attachment_id', string="SOW", help="Optional - not every deal has one.")

    def _mz_check_assign_type_allowed(self, vals):
        """Server-side backstop for x_assign_type in ('team', 'internal'): the view
        only offers those to whoever passes x_can_use_assign_radio (DMT team
        membership, MD, or CTO/Admin) AND holds ATL/TL/Manager tier
        (x_can_assign_beyond_self), and the onchange bounces anyone else back to
        'self' and clears user_id if it falls outside x_assignable_user_ids - but
        all of that is UI-only, so a direct RPC/API write could still set either.
        Raises the same way the UI would have refused, instead of silently
        accepting it. Mirrors _compute_x_assignable_user_ids' DMT waiver (a DMT
        team member skips the tier gate entirely and gets the full team roster as
        their pool for both 'team' and 'internal') and its 'internal' pool for
        everyone else (_mz_team_subordinate_group_users) - keep all three in
        sync, or a UI selection could get rejected on save.

        Own-team ATL/TL/Manager exception (2026-09-15, client instruction:
        "internal radio button should be selecteable in order to assign
        salesperson inside their team, that is only for managers, tl's and
        atl's") - partially reverses the 2026-09-08 "Team/Internal is DMT+CTO/
        Admin+MD only" rule, but ONLY for 'Internal', and ONLY within their own
        team: an ATL/TL/Manager may now use Internal to assign a salesperson on
        a lead that's already on their OWN team (most commonly an unowned lead
        just handed off from DMT), without needing x_can_use_assign_radio at
        all. 'Team' itself (cross-team routing) stays DMT/CTO/Admin/MD-only -
        unaffected. Checked per-record below (team_id might differ per lead in
        a bulk write, though in practice this is always a single lead)."""
        assign_type = vals.get('x_assign_type')
        if assign_type not in ('team', 'internal') or self.env.su:
            return
        user = self.env.user
        user_is_dmt = self._mz_user_is_dmt(user)
        can_use_radio = self._mz_user_can_use_assign_radio(user)
        is_cto_admin = user.has_group('mazenet_access_rights.group_mzr_cto_admin')
        tier, _chain = self._mz_user_tier_chain(user)
        own_team = self._mz_user_own_team(user)
        # is_cto_admin added 2026-09-12 alongside _compute_x_assignable_user_ids' own
        # can_beyond_self - CTO/Admin belong to no crm.team of their own so tier is
        # always None for them, which used to fail this check even though the UI
        # error message below (and _mz_user_can_use_assign_radio) already claimed to
        # allow it. MD is deliberately NOT included - still Self-only everywhere else.
        for record in (self or [self.env['crm.lead']]):
            team_id = vals['team_id'] if 'team_id' in vals else (record.team_id.id if record else False)
            # _mz_is_corp_manager_oversight_team OR covers Corporate BU Manager's
            # 5-team oversight (2026-09-22) - see that method's own docstring; kept
            # in sync with _compute_x_assignable_user_ids' own can_assign_within_own_team.
            own_team_internal_ok = bool(
                assign_type == 'internal' and tier in ('atl', 'tl', 'manager') and team_id
                and (
                    (own_team and team_id == own_team.id)
                    or self._mz_is_corp_manager_oversight_team(user, self.env['crm.team'].browse(team_id))
                )
            )
            if not own_team_internal_ok and (
                not can_use_radio or not (user_is_dmt or is_cto_admin or tier in ('atl', 'tl', 'manager'))
            ):
                raise AccessError(_(
                    "Only DMT team members, CTO/Admin, or an ATL/TL/Manager on this "
                    "lead's own team (Internal only) can assign to a team or assign "
                    "internally. Everyone else - including MD - can only assign to "
                    "themselves."))

            if not vals.get('user_id'):
                continue
            team = self.env['crm.team'].browse(team_id) if team_id else self.env['crm.team']
            if user_is_dmt or is_cto_admin:
                pool_ids = team.member_ids.ids
            elif assign_type == 'team':
                pool_ids = team.create_lead_id.ids
            else:
                pool_ids = self._mz_team_subordinate_group_users(team, user).ids
                if not pool_ids:
                    # Same crm.team.privelege_ids-is-never-configured fallback as
                    # _compute_x_assignable_user_ids (see _mz_reports_to_users'
                    # own docstring) - keep both in sync.
                    pool_ids = self._mz_reports_to_users(user).ids
            if vals['user_id'] not in pool_ids:
                raise AccessError(_(
                    "The selected salesperson isn't in the allowed assignment pool for "
                    "this team under '%s' assignment. Pick from the assignable list."
                ) % assign_type)

    @api.model
    def _mz_stage_gate_rules_resolved(self, bu_category):
        """[(stage record, [mandatory field names]), ...] in sequence order for a BU,
        resolved from MZ_STAGE_GATE_RULES's xmlids. Empty list for a BU with no rules
        configured yet, or an xmlid that doesn't (or doesn't yet) resolve."""
        rules = MZ_STAGE_GATE_RULES.get(bu_category)
        if not rules:
            return []
        resolved = []
        for xmlid, field_names in rules:
            stage = self.env.ref(f'mazenet_crm.{xmlid}', raise_if_not_found=False)
            if stage:
                resolved.append((stage, field_names))
        return resolved

    def _mz_resolve_gate_value(self, fname, vals):
        """The would-be value of `fname` after `vals` is applied, for mandatory-ness
        purposes. Plain fields: the raw vals entry (or current value if untouched).
        Many2many (the multi-file attachment fields): raw (6,0,ids)/(4,id)/... write
        commands aren't truthy/falsy in a way that reflects the resulting record set
        (e.g. a bare (6,0,[]) command is itself a non-empty list even though it clears
        the field), so replay the commands against the current ids instead."""
        if fname not in vals:
            return self[fname]
        field = self._fields[fname]
        if field.type != 'many2many':
            return vals[fname]
        ids = set(self[fname].ids)
        for command in vals[fname]:
            op = command[0]
            if op == 6:
                ids = set(command[2])
            elif op == 5:
                ids = set()
            elif op == 4:
                ids.add(command[1])
            elif op in (2, 3):
                ids.discard(command[1])
            elif op == 0:
                ids.add(-1)  # new record being created inline - treat as filled
        return ids

    def _mz_missing_mandatory_fields(self, field_names, vals):
        """Names of fields in `field_names` that are still empty, considering `vals`
        (what's being written in this same call) over the record's current stored value.
        Special-cased for 'source_id': when the (about-to-be-set) source requires a
        companion reference text (utm.source.x_requires_reference_text - Referral/Ads/
        GeM Bid style sources), 'referred' must be filled too even though it isn't its
        own entry in MZ_STAGE_GATE_RULES (it's conditional on the source, not always
        mandatory). Also special-cased for MZ_EITHER_OR_MANDATORY_FIELDS pseudo-names
        (e.g. 'phone_or_email'): satisfied if ANY of the alternative fields is filled,
        not each one individually. And for MZ_WAIVER_CONDITIONAL_MANDATORY_FIELDS
        (Corporate Evaluation stage): not required at all once its paired waiver
        checkbox (Pre-Approved / Mazenet Validation) is ticked. Also for
        MZ_SELECTION_CONDITIONAL_MANDATORY_FIELDS (LMS Delivery stage): only required
        when its companion Selection field equals one specific value (e.g. TOC By only
        matters when Training Content = New Content). And for
        MZ_BOOLEAN_CONDITIONAL_MANDATORY_FIELDS (TNH Proposal stage): the OPPOSITE
        direction from the waiver dict - only required when its trigger boolean is
        True (e.g. Deviation Notes only matters once Deviation is ticked)."""
        self.ensure_one()
        missing = []
        for fname in field_names:
            if fname in MZ_EITHER_OR_MANDATORY_FIELDS:
                alt_fields = MZ_EITHER_OR_MANDATORY_FIELDS[fname]
                if not any(self._mz_resolve_gate_value(f, vals) for f in alt_fields):
                    missing.append(fname)
                continue
            if fname in MZ_WAIVER_CONDITIONAL_MANDATORY_FIELDS:
                waiver_field = MZ_WAIVER_CONDITIONAL_MANDATORY_FIELDS[fname]
                if self._mz_resolve_gate_value(waiver_field, vals):
                    continue
            if fname in MZ_SELECTION_CONDITIONAL_MANDATORY_FIELDS:
                trigger_field, trigger_value = MZ_SELECTION_CONDITIONAL_MANDATORY_FIELDS[fname]
                if self._mz_resolve_gate_value(trigger_field, vals) != trigger_value:
                    continue
            if fname in MZ_BOOLEAN_CONDITIONAL_MANDATORY_FIELDS:
                trigger_field = MZ_BOOLEAN_CONDITIONAL_MANDATORY_FIELDS[fname]
                if not self._mz_resolve_gate_value(trigger_field, vals):
                    continue
            value = self._mz_resolve_gate_value(fname, vals)
            if not value:
                missing.append(fname)
        if 'source_id' in field_names:
            source_id = vals['source_id'] if 'source_id' in vals else self.source_id.id
            if source_id and self.env['utm.source'].browse(source_id).x_requires_reference_text:
                referred = vals['referred'] if 'referred' in vals else self.referred
                if not referred:
                    missing.append('referred')
        return missing

    def _mz_gate_field_label(self, fname):
        """Human-readable label for a mandatory-field name in a stage-gate error
        message - handles MZ_EITHER_OR_MANDATORY_FIELDS pseudo-names (e.g.
        'phone_or_email' -> 'Phone / Email') as well as real field names."""
        if fname in MZ_EITHER_OR_MANDATORY_FIELDS:
            return ' / '.join(self._fields[f].string for f in MZ_EITHER_OR_MANDATORY_FIELDS[fname])
        return self._fields[fname].string

    def _mz_stage_gate_check(self, new_stage, vals):
        """Raise UserError if moving to `new_stage` skips past a stage (in this lead's
        BU) whose own mandatory fields (MZ_STAGE_GATE_RULES) aren't filled yet - "Build
        the fields in M2, enforce the Mandatory column on stage change in M3" from the
        pipeline sheets. Only a FORWARD move (to a later stage) is gated; moving
        backward never is. Jumping straight past several stages checks every stage in
        between, not just the one immediately before `new_stage`.

        Phone/Email (MZ_EITHER_OR_MANDATORY_FIELDS's 'phone_or_email') is checked
        separately here, on EVERY forward move regardless of BU or current stage -
        unlike the rest of MZ_STAGE_GATE_RULES it isn't tied to one specific stage
        being passed through, since either field can be blanked out again well
        after the first stage that required it."""
        self.ensure_one()
        if not new_stage:
            return
        current_stage = self.stage_id
        is_forward_move = bool(current_stage) and new_stage.sequence > current_stage.sequence

        problems = []
        if is_forward_move:
            missing = self._mz_missing_mandatory_fields(['phone_or_email'], vals)
            if missing:
                problems.append(_("Every stage: %s") % self._mz_gate_field_label('phone_or_email'))

        team = self.team_id
        if team:
            resolved = self._mz_stage_gate_rules_resolved(team.x_bu_category)
            stage_ids = [s.id for s, _fields in resolved]
            if new_stage.id in stage_ids:
                new_index = stage_ids.index(new_stage.id)
                current_index = stage_ids.index(current_stage.id) if current_stage.id in stage_ids else -1
                if new_index > current_index:
                    for stage, field_names in resolved[max(current_index, 0):new_index]:
                        missing = self._mz_missing_mandatory_fields(field_names, vals)
                        if missing:
                            labels = ', '.join(self._mz_gate_field_label(f) for f in missing)
                            problems.append(f"{stage.name}: {labels}")

        if problems:
            raise UserError(_(
                "'%(lead)s' can't move to '%(target)s' yet - required fields are still "
                "empty:\n%(details)s"
            ) % {'lead': self.name, 'target': new_stage.name, 'details': '\n'.join(problems)})

    def _mz_won_gate_check(self, new_stage, vals):
        """Raise UserError if `new_stage` is a Won stage and this lead's BU
        (MZ_WON_GATE_RULES) still has a required attachment missing - Corporate's
        3-document gate (tagged Quotation + both POs), LMS's 1-document gate,
        TNH's 2-document gate. No entry in MZ_WON_GATE_RULES for a BU makes this
        a no-op for it (dmt/tally/tech/swdev/mis are unaffected).

        Called from write() itself, NOT from action_set_won() (which used to be
        the only place this was enforced, and got removed) - a plain kanban
        drag-and-drop straight onto the Won/Lost stage column never goes
        through action_set_won() at all, it's just an ordinary write({'stage_id':
        ...}) that stock's own won_status computed field then reacts to. Stock's
        action_set_won() itself is ALSO just a write({'stage_id': won_stage.id,
        ...}) under the hood (addons/crm/models/crm_lead.py) - the exact same
        path a drag takes - so this one check in write() now catches both,
        where the old action_set_won()-only override caught neither a drag nor
        (silently) the client's own real bug report."""
        self.ensure_one()
        if not new_stage or not new_stage.is_won:
            return
        required = MZ_WON_GATE_RULES.get(self.team_id.x_bu_category)
        if not required:
            return
        missing = [f for f in required if not self._mz_resolve_gate_value(f, vals)]
        if missing:
            labels = ', '.join(self._mz_gate_field_label(f) for f in missing)
            raise UserError(_(
                "'%(lead)s' can't move to '%(target)s' yet - missing required "
                "document(s): %(labels)s."
            ) % {'lead': self.name, 'target': new_stage.name, 'labels': labels})

    @api.model
    def _mz_backfill_x_dmt_originated(self):
        """Data-file hook (data/teams.xml's own <function> call, NOT a
        post_init_hook - post_init_hook only fires on a fresh install, never on
        a plain -u upgrade of an already-installed module, which is exactly the
        upgrade path this fix needed to run through). x_dmt_originated is only
        ever set going forward, by create() - existing leads already sitting in
        team_dmt when this field was introduced (2026-09-08, fixing the
        AccessError a DMT agent hit transferring a lead to another team) would
        otherwise never get it, and lose all read access the moment they're
        transferred out, same as before the fix. One-time-in-effect backfill:
        every lead CURRENTLY in team_dmt originated from DMT by definition.
        Idempotent (only touches x_dmt_originated=False rows) so re-running it
        on every future -u upgrade is harmless."""
        dmt_team = self.env.ref('mazenet_crm.team_dmt', raise_if_not_found=False)
        if not dmt_team:
            return
        leads = self.sudo().with_context(active_test=False).search([
            ('team_id', '=', dmt_team.id), ('x_dmt_originated', '=', False),
        ])
        if leads:
            leads.write({'x_dmt_originated': True})

    # BU Manager each team's TL reports to (res_users.x_reports_to_id) - Hunter,
    # Account Manager, Corp Training Delivery, LMS and TNH all share ONE Corporate
    # BU Manager (mazenet_access_rights' MZR_TIER_GROUP_CHAINS already encodes
    # this: all five chains' Manager tier is group_mzr_corporate_manager); every
    # other BU has its own dedicated Manager. team_corporate itself (the Corporate
    # BU Manager's own team, holding just them + their 2 direct agents) maps to
    # None - there's no BU above a BU Manager in this system.
    MZ_REPORTS_TO_BU_MANAGER_LOGIN = {
        'team_dmt': 'dmt.mgr@test.mazenet',
        'team_tally': 'tally.mgr@test.mazenet',
        'team_technology': 'tech.mgr@test.mazenet',
        'team_software': 'swdev.mgr@test.mazenet',
        'team_mis': 'mis.mgr@test.mazenet',
        'team_hunter': 'corp.mgr@test.mazenet',
        'team_account_manager': 'corp.mgr@test.mazenet',
        'team_corp_training_delivery': 'corp.mgr@test.mazenet',
        'team_lms': 'corp.mgr@test.mazenet',
        'team_tnh': 'corp.mgr@test.mazenet',
        'team_corporate': None,
    }

    @api.model
    def _mz_backfill_x_reports_to_hierarchy(self):
        """Data-file hook (data/migrations.xml's own <function> call - see
        _mz_backfill_x_dmt_originated just above for why a plain post_init_hook
        can't be used instead). Populates res.users.x_reports_to_id for every
        demo user, resolved purely from each team's own member_ids (never
        guessed from login text at runtime) - client instruction 2026-09-21:
        "im tl1... dropdown should show only my agents who are reporting to
        me" - the Salesperson pool (_mz_reports_to_users) needs a REAL
        reporting-line field to answer that, since crm.team's own tier groups
        only say WHAT TIER a user is, never WHO SPECIFICALLY they report to
        (every Hunter agent, ATL-1's or ATL-2's, shares one flat
        group_mzr_hunter_agent group - see x_reports_to_id's own help text).

        Three rules, applied per team (plus team_corporate, for the Corporate
        BU Manager's own direct agents):
        - Agent tier (matched by login ending in '.agentN', NOT
          _mz_user_tier_chain - TL-direct and Manager-direct agents use their
          own distinct mazenet_access_rights groups, e.g.
          group_mzr_hunter_tl_direct_agent, which _mz_user_tier_chain doesn't
          recognize at all, returning no tier for them): reports to whichever
          OTHER active user's login is identical minus that '.agentN' suffix
          (an ATL's own agent -> that ATL; a TL's or Manager's own direct
          agent -> that TL/Manager directly - one rule covers both shapes).
        - ATL tier: reports to the team's own TL - matched by a shared 'tdl'/
          'sales' substring for Tally specifically (its ONLY multi-TL team:
          two full parallel chains, TDL and Sales, sharing one BU Manager),
          the team's single TL everywhere else.
        - TL tier: reports to MZ_REPORTS_TO_BU_MANAGER_LOGIN's entry for their
          team - the shared Corporate BU Manager for Hunter/AM/Corp Training/
          LMS/TNH, their own dedicated Manager for every other BU.
        - Manager tier, and MD (who holds no team membership at all so never
          reaches the per-team loop below): reports to CTO. Confirmed against
          the client's own Phase-1 access-roster reference (2026-09-21) -
          every BU Manager and MD report to CTO there; an earlier version of
          this method left them unset ("top of chain, no one above"), which
          the roster showed was wrong for these two roles specifically - CTO
          itself is the only genuine top of this system's chain (still left
          unset; it holds no supervisor in that same reference).

        Idempotent (a plain write of the same value each time), so re-running
        it on every future -u upgrade is harmless - and self-healing if a
        team's roster changes later, since it's recomputed from team.member_ids
        fresh each time rather than a one-time hardcoded snapshot."""
        import re
        Users = self.env['res.users'].sudo()
        team_xmlids = list(self.MZ_REPORTS_TO_BU_MANAGER_LOGIN.keys())
        all_logins = {u.login: u for u in Users.search([])}
        cto_user = all_logins.get('cto@test.mazenet')
        updates = {}  # user -> reports_to user (or False)

        md_user = all_logins.get('md@test.mazenet')
        if md_user and cto_user:
            updates[md_user] = cto_user

        for team_xmlid in team_xmlids:
            team = self.env.ref(f'mazenet_crm.{team_xmlid}', raise_if_not_found=False)
            if not team:
                continue
            members = team.member_ids
            member_logins = set(members.mapped('login'))
            for member in members:
                login = member.login
                if re.search(r'\.agent\d+@', login):
                    stripped = re.sub(r'\.agent\d+@', '@', login)
                    superior = all_logins.get(stripped)
                    if superior:
                        updates[member] = superior
                    continue
                tier, _chain = self._mz_user_tier_chain(member)
                if tier == 'atl':
                    candidates = [
                        l for l in member_logins
                        if l in all_logins and self._mz_user_tier_chain(all_logins[l])[0] == 'tl'
                    ]
                    if 'tdl' in login:
                        candidates = [l for l in candidates if 'tdl' in l]
                    elif 'sales' in login:
                        candidates = [l for l in candidates if 'sales' in l]
                    if len(candidates) == 1:
                        updates[member] = all_logins[candidates[0]]
                elif tier == 'tl':
                    manager_login = self.MZ_REPORTS_TO_BU_MANAGER_LOGIN.get(team_xmlid)
                    if manager_login and manager_login in all_logins:
                        updates[member] = all_logins[manager_login]
                elif tier == 'manager' and cto_user:
                    updates[member] = cto_user
                # CTO itself: left unset, the genuine top of this system's chain.

        for user, superior in updates.items():
            if user.x_reports_to_id != superior:
                user.x_reports_to_id = superior

    @api.model_create_multi
    def create(self, vals_list):
        # CTO/Admin, MD and Corporate BU Manager have no create access at all (client
        # instruction, 2026-09-21: "no need create lead access... hide the New button
        # for them"; extended to Corporate BU Manager same day - same reasoning, since
        # corp.mgr's own cross-team oversight role mirrors CTO/Admin/MD's rather than a
        # normal team manager's). This has to be an explicit guard, not just an
        # ir.rule perm_create=False - CTO/Admin already qualifies for perm_create=True
        # through dozens of OTHER teams' own rules via implied_ids (confirmed live:
        # setting perm_create=False on CTO/Admin's own global rule alone did NOT block
        # creation), and all three groups hold base.group_user, whose own crm.lead ACL
        # row already grants create=1 model-wide - none can be "subtracted" from for
        # one subgroup via ir.model.access.csv either, since ACL rows OR-combine
        # across a user's groups. The New button itself is hidden by a separate,
        # THIRD mechanism - see mz_crm_lead_kanban_no_create_cto_md and its sibling
        # list views (views/crm_lead_views.xml) - since the list/kanban "create" arch
        # attribute is a static per-view boolean, not a per-user expression. Keep this
        # list of three groups in sync with ir_actions_act_window.py's
        # _MZ_NO_CREATE_VIEW_MAP check.
        if not self.env.su and (
            self.env.user.has_group('mazenet_access_rights.group_mzr_cto_admin')
            or self.env.user.has_group('mazenet_access_rights.group_mzr_md')
            or self.env.user.has_group('mazenet_access_rights.group_mzr_corporate_manager')
        ):
            raise AccessError(_("CTO/Admin, MD and Corporate BU Manager cannot create leads."))
        dmt_team = self.env.ref('mazenet_crm.team_dmt', raise_if_not_found=False)
        for vals in vals_list:
            self._mz_check_assign_type_allowed(vals)
            # No-Teamless-Lead Guarantee (2026-09-11, narrowed 2026-09-12): only kicks
            # in when 'team_id' is entirely ABSENT from vals - a creation path that
            # never resolved a team at all (an import, an API call, an incoming-email
            # lead, or a plain Agent creating outside the normal form; hit live on
            # staging, lead id 1406, "GH 100" - "not assigned to any team" was stock
            # CRM's OWN chatter message). Every ir.rule, stage domain, and kanban
            # grouping in this module assumes team_id is always set for THOSE cases,
            # so falling back to DMT (this module's designated catch-all intake team,
            # mirroring _mz_default_team_id's own CTO/Admin/MD case) still applies.
            #
            # Deliberately NOT applied when 'team_id' is explicitly present as False -
            # that's the normal, INTENDED shape of a CTO/Admin/MD Self-assigned lead
            # (own_team is empty for them, and team_id's own required="x_assign_type
            # != 'self'" in the view already says empty is fine for Self) - silently
            # overriding that explicit choice with DMT was itself the bug (hit live
            # 2026-09-12: a CTO's Self-assigned lead saved with Sales Team = DMT even
            # though nobody ever picked a team).
            if 'team_id' in vals:
                team_id = vals['team_id']
            else:
                team_id = self._mz_default_team_id()
                if not team_id and dmt_team:
                    team_id = dmt_team.id
                vals['team_id'] = team_id
            if dmt_team and 'x_dmt_originated' not in vals:
                vals['x_dmt_originated'] = team_id == dmt_team.id
        records = super(CrmLead, self).create(vals_list)
        records.filtered(
            lambda l: l.x_lms_training_start_date or l.x_lms_training_end_date
        )._mz_sync_lms_weeks()
        return records

    def write(self, vals):
        u = self.env.user
        is_cto_admin = u.has_group("mazenet_access_rights.group_mzr_cto_admin")
        self._mz_check_assign_type_allowed(vals)

        # Assign/Reassign Notification (client instruction, 2026-09-09): capture the
        # PRE-write owner/team so it can be compared against the post-write value below,
        # once super().write() actually applies it - vals['user_id']/['team_id'] being
        # present doesn't guarantee the value actually CHANGED (e.g. re-saving the same
        # salesperson), so the comparison has to happen after the write, not from vals.
        track_assign = 'user_id' in vals or 'team_id' in vals
        if track_assign:
            pre_assign = {lead.id: (lead.user_id, lead.team_id) for lead in self}

        # Direct Lead Archiving Restriction: leads are archived only via the Archive Lead
        # Wizard (which stamps mz_archive_wizard on the context), never a raw active=False.
        if "active" in vals and not vals["active"]:
            if not self.env.su and not is_cto_admin and not self.env.context.get("mz_archive_wizard"):
                raise AccessError(_("Leads can only be archived by CTO / Admin via the Archive Lead Wizard."))

        if not self.env.su and not is_cto_admin:
            # MD Restriction: otherwise read-only, except for leads MD created for
            # themselves (create() enforces the same "own only" rule) - every other
            # lead stays read-only for MD. _mz_can_edit_owned carries the matching
            # waiver so the team-membership check below doesn't also block this
            # (MD isn't a member of any crm.team by design).
            if u.has_group("mazenet_access_rights.group_mzr_md"):
                for lead in self:
                    if lead.user_id != u:
                        raise AccessError(_(
                            "MD role can only edit leads they created themselves; "
                            "every other lead is read-only."))

            content_touched = set(vals.keys()) - SYSTEM_FIELDS

            # RED Lock Enforcement: a locked lead is read-only until released via
            # action_release_lock() - EXCEPT for whoever is authorized to RELEASE it
            # (can_user_release_lock: the owner's head group - one tier above the
            # owner's own tier - or CTO/Admin; a Manager-tier owner self-releases).
            # Deliberately NOT _mz_can_edit_by_team here: that check only asks "is this
            # user ATL/TL/Manager on the CURRENT team_id", which doesn't exclude the
            # locked owner themselves if they happen to hold ATL/TL/Manager tier, and
            # doesn't require them to be the owner's specific superior either - either
            # gap would let the very person the lock is meant to freeze (or an unrelated
            # peer ATL/TL) keep editing. Using can_user_release_lock keeps "who can edit
            # while locked" and "who can release the lock" the same person, which is the
            # actual intent (e.g. an ATL who missed a meeting gets RED-locked and can no
            # longer edit their own lead even though they're ATL-tier; only their TL can
            # edit/release it). Only bookkeeping/system fields (chatter, activities, and
            # the lock fields themselves - so the release action can clear them) are
            # exempt regardless.
            #
            # Team-Transfer Enforcement: the SAME team_id-scoping applies even when the
            # lead isn't locked - stock CRM's own "Sales: All Documents" ir.rule
            # (granted to every staging user for CRM-menu/team visibility, see
            # feedback_sales_group_visibility memory) is unrestricted, so
            # record_rules.xml's team-scoped write rules no longer actually gate
            # anything on their own. Sales Team (team_id) is the single source of
            # truth for both the transfer action and this check, same as it is for
            # RED-lock escalation - there is NO owner exemption here: once team_id
            # moves off wherever gave someone access, it's read-only for them too,
            # owner included (_mz_can_edit_owned). That's different from the LOCKED
            # branch just above, where the owner is deliberately excluded even on
            # their OWN team - being locked out is the whole point of RED lock for
            # them specifically.
            # DMT Reassignment Waiver REMOVED (client instruction, 2026-09-11, same
            # change as _mz_can_edit_owned above): a transferred/locked lead is now
            # fully read-only for DMT too, including user_id - no more carve-out for
            # reassigning the salesperson on a lead DMT no longer has any claim to.

            if content_touched:
                for lead in self:
                    if lead.x_is_locked:
                        if not lead.can_user_release_lock(u):
                            raise AccessError(_(
                                "Lead '%s' is RED-locked and read-only. Use 'Release RED Lock' "
                                "before it can be edited again."
                            ) % lead.name)
                    elif not lead._mz_can_edit_owned(u):
                        # Same distinction as _compute_x_team_transfer_readonly
                        # (2026-09-11 fix): "not on this team at all" (a genuine
                        # transfer) and "on the team but not the owner, without
                        # ATL/TL/Manager tier" are different problems with different
                        # fixes - conflating them under one "transferred" message
                        # was actively misleading (hit live 2026-09-11: a DMT Agent
                        # got told a peer-owned, never-transferred DMT lead had
                        # "been transferred to another team").
                        if not lead.team_id or u not in lead.team_id.member_ids:
                            raise AccessError(_(
                                "Lead '%s' has been transferred to another team and is "
                                "read-only for you now."
                            ) % lead.name)
                        raise AccessError(_(
                            "Lead '%s' is owned by someone else on your team. Only the "
                            "owner, or an ATL/TL/Manager, can edit it."
                        ) % lead.name)

            # Salesperson Assignment Restriction (client instruction, 2026-09-13):
            # assigning a salesperson to an UNOWNED lead within one's own team -
            # most commonly right after a cross-team handoff lands it there with no
            # owner yet - is an ATL/TL/Manager-only action, the same tier
            # _mz_check_assign_type_allowed already requires for Team/Internal. A
            # plain Agent can still edit an unowned lead's OTHER fields
            # (_mz_can_edit_owned's own "not self.user_id" branch lets any team
            # member through for that), just not decide who gets it. Skipped
            # whenever 'x_assign_type' is ALSO in vals - that's DMT/CTO/Admin/MD's
            # own assign-type flow, already validated by _mz_check_assign_type_
            # allowed above; this only targets a DIRECT user_id edit outside that
            # flow (e.g. a Tally TL picking a Salesperson via the plain field once
            # DMT's handed them a lead). DMT is exempt entirely - membership alone
            # already waives every tier gate for DMT elsewhere in this file
            # (_compute_x_assignable_user_ids), and a non-member wouldn't have
            # reached this point at all (_mz_can_edit_owned already checked that).
            if 'user_id' in vals and 'x_assign_type' not in vals and not self._mz_user_is_dmt(u):
                tier, _chain = self._mz_user_tier_chain(u)
                if tier not in ('atl', 'tl', 'manager'):
                    for lead in self:
                        if not lead.user_id and lead.team_id:
                            raise AccessError(_(
                                "Only an ATL/TL/Manager can assign a Salesperson to "
                                "'%s'."
                            ) % lead.name)

            # BU Manager Content Lock REMOVED (client instruction, 2026-09-09): the
            # hierarchy is Manager full rights, TL full rights, ATL full rights - a
            # Manager editing an Agent's lead directly (e.g. Tally Manager on a Tally
            # Prime Upgrade Agent's lead) is normal, not something to route through the
            # TL first. Manager already reaches here via _mz_can_edit_by_team/
            # _mz_can_edit_owned like any other ATL/TL/Manager on the lead's current
            # team - no separate content-vs-reassign restriction on top of that.

        # M3 stage-mandatory-field gate: enforced regardless of role (CTO/Admin included -
        # this is a data-completeness rule, not an authority one), skipped only for raw
        # su/system writes (migrations, demo-data seeding) so those aren't forced to
        # pre-fill every mandatory field for stages they're placing records into directly.
        # Same reasoning for the Won-gate right alongside it (_mz_won_gate_check) - it
        # used to live only in action_set_won(), which a plain kanban drag onto the
        # Won/Lost column never actually calls, silently skipping the document check.
        if 'stage_id' in vals and not self.env.su:
            new_stage = self.env['crm.stage'].browse(vals['stage_id'])
            for lead in self:
                lead._mz_stage_gate_check(new_stage, vals)
                lead._mz_won_gate_check(new_stage, vals)

        # DMT Transfer Completeness Gate (client instruction, 2026-09-13): before a
        # DMT-owned lead's team_id moves away from DMT, verify every mandatory field
        # across DMT's OWN pipeline (New Lead, Lead Validation, Transfer to BU) is
        # actually filled. The M3 gate just above only fires when 'stage_id' is
        # explicitly in vals - a plain team handoff never sets that itself (the
        # Cross-Team Handoff Stage Advance below sets stage_id AFTERWARD, by which
        # point team_id already belongs to the RECEIVING team, so
        # _mz_stage_gate_check would validate against THEIR rules instead of DMT's -
        # completely missing DMT's own requirements). Checked against
        # stage_dmt_transferred as the notional target - DMT's own last real stage -
        # so every stage before it gets validated, same as actually progressing
        # through DMT's funnel normally would have enforced. Same "regardless of
        # role" reasoning as the M3 gate above - a data-completeness rule, not an
        # authority one, so CTO/Admin isn't exempt either, only raw su/system writes.
        if 'team_id' in vals and not self.env.su:
            dmt_team = self.env.ref('mazenet_crm.team_dmt', raise_if_not_found=False)
            stage_dmt_transferred = self.env.ref(
                'mazenet_crm.stage_dmt_transferred', raise_if_not_found=False
            )
            if dmt_team and stage_dmt_transferred:
                for lead in self:
                    if lead.team_id == dmt_team and vals['team_id'] != dmt_team.id:
                        lead._mz_stage_gate_check(stage_dmt_transferred, vals)

        result = super(CrmLead, self).write(vals)

        if 'x_lms_training_start_date' in vals or 'x_lms_training_end_date' in vals:
            self._mz_sync_lms_weeks()

        # Cross-Team Handoff Stage Advance (2026-09-12, REPLACES the DMT-only
        # "Handoff Auto-Advance" from 2026-09-11): whenever a lead's team_id
        # changes to a DIFFERENT team (without an explicit accompanying stage_id
        # in the same write), advance the REAL stage_id to the RECEIVING team's
        # own entry stage (_mz_team_entry_stage) - keeps stage_id itself always
        # meaning "real progress within the lead's CURRENT team", the single
        # source of truth every ir.rule/kanban/stage-gate/report in this module
        # already assumes it is.
        #
        # The previous version only fired for leads coming FROM DMT and forced
        # them onto DMT's OWN "Follow-up's" stage instead - that correctly made
        # DMT's kanban show it as done-from-their-side, but the same real
        # stage_id change leaked right back in as a phantom foreign column on
        # the RECEIVING team's OWN kanban the instant they could see the record
        # (_read_group_stage_ids's team-filter only hides EMPTY foreign columns,
        # never one a real record is actually sitting in - see its docstring).
        # DMT's "Follow-up's" display is now handled separately and per-viewer,
        # via x_dmt_pipeline_stage_id and DMT's own dedicated Pipeline view/
        # action - it no longer needs to touch the real stage_id at all, so the
        # receiving team is free to get a real, correctly-scoped entry stage
        # here instead.
        #
        # sudo() + batched by target stage; skip if the caller already set
        # stage_id explicitly (respects an explicit override - this write is a
        # one-time system-driven convenience, not a user stage change, same
        # reasoning as the x_dmt_originated retire-write below).
        if track_assign and 'team_id' in vals and 'stage_id' not in vals:
            moved = self.filtered(
                lambda l: l.team_id and pre_assign.get(l.id, (None, l.team_id))[1] != l.team_id
            )
            by_stage = {}
            for lead in moved:
                entry_stage = self._mz_team_entry_stage(lead.team_id)
                if entry_stage and lead.stage_id != entry_stage:
                    by_stage.setdefault(entry_stage.id, self.env['crm.lead'])
                    by_stage[entry_stage.id] |= lead
            for entry_stage_id, leads in by_stage.items():
                leads.sudo().write({'stage_id': entry_stage_id})

            # Post-Handoff Assign-Type Reset (2026-09-15 fix: "when selecting self
            # radio button salesperson automatically should be as logged in user" -
            # reported broken for a regular team's own TL/ATL/Manager on a lead just
            # handed off to them). The real x_assign_type stays 'team' after a
            # handoff (that's what triggered the move) - but 'Team' isn't even a
            # selectable option once the lead lands on a REGULAR (non-DMT) team;
            # x_assign_type_no_team's own compute maps 'team' to 'self' for DISPLAY
            # ONLY, so the radio visually shows Self already selected without the
            # viewer ever having to click it - meaning its inverse (the thing that
            # actually triggers assign_salesperson's onchange and sets user_id) never
            # fires, and Salesperson silently stays unset instead of auto-filling to
            # whoever opens it. Reset the REAL x_assign_type to 'self' here instead -
            # a one-time handoff convenience, same as the stage advance just above -
            # so the stored value actually matches what's displayed from the start.
            # DMT is exempt: a lead moving TO DMT still shows the full 3-option field
            # there, so no display/reality mismatch exists in that direction.
            reset_dmt_team = self.env.ref('mazenet_crm.team_dmt', raise_if_not_found=False)
            to_reset_assign_type = moved.filtered(
                lambda l: l.x_assign_type == 'team' and l.team_id != reset_dmt_team
            )
            if to_reset_assign_type:
                to_reset_assign_type.sudo().write({'x_assign_type': 'self'})

        # DMT Details Snapshot (client instruction, 2026-09-13): the instant a
        # DMT-originated lead's team_id first moves OFF DMT, freeze a copy of DMT's
        # own Pipeline Fields tab into the x_dmt_snap_* fields above - see their own
        # comment for why (source_id/x_product_service are shared with the
        # receiving team's own stage-gate fields and get overwritten for real once
        # that team starts working the lead). Guarded by x_dmt_snap_captured so a
        # LATER handoff between two other teams never overwrites DMT's original
        # answers with whatever the first receiving team has since put in those
        # shared fields. Per-lead (each one's own field values differ), but this is
        # a single-handoff action in practice, never a bulk one.
        if track_assign and 'team_id' in vals:
            dmt_team = self.env.ref('mazenet_crm.team_dmt', raise_if_not_found=False)
            if dmt_team:
                to_snapshot = self.filtered(
                    lambda l: pre_assign.get(l.id, (None, l.team_id))[1] == dmt_team
                    and l.team_id != dmt_team
                    and not l.x_dmt_snap_captured
                )
                for lead in to_snapshot:
                    lead.sudo().write({
                        'x_dmt_snap_organic_inorganic': lead.x_organic_inorganic,
                        'x_dmt_snap_source_id': lead.source_id.id,
                        'x_dmt_snap_referred': lead.referred,
                        'x_dmt_snap_company_or_individual': lead.x_company_or_individual,
                        'x_dmt_snap_contact_purpose': lead.x_contact_purpose,
                        'x_dmt_snap_product_service': lead.x_product_service,
                        'x_dmt_snap_employee_count': lead.x_employee_count,
                        'x_dmt_snap_company_turnover': lead.x_company_turnover,
                        'x_dmt_snap_target_team_id': lead.x_target_team_id.id,
                        'x_dmt_snap_transfer_notes': lead.x_transfer_notes,
                        'capture_dmt_lead_id': self.env.uid, #capture the user of the Dmt team
                        'x_dmt_snap_captured': True,
                    })

        # x_dmt_originated self-expiry (2026-09-11): the first time someone OUTSIDE
        # DMT substantively edits a lead DMT originated, permanently retire the flag
        # - see record_rules.xml's rule_crm_lead_dmt_originated_read/_write for why a
        # static ir.rule domain condition (e.g. keyed off stage_id) can't do this
        # safely instead (stock CRM resets stage_id in the SAME write that changes
        # team_id, so a stage-based condition would break the handoff write itself).
        # sudo() here is deliberate and safe: it only ever flips this one flag to
        # False, and self.env.su on the recursive write() call short-circuits every
        # guard above keyed on "not self.env.su", so this can't recurse further.
        if not self.env.su and not self._mz_user_is_dmt(u):
            if set(vals.keys()) - SYSTEM_FIELDS:
                to_retire = self.filtered('x_dmt_originated')
                if to_retire:
                    to_retire.sudo().write({'x_dmt_originated': False})

        if track_assign:
            for lead in self:
                old_user, old_team = pre_assign.get(lead.id, (lead.user_id, lead.team_id))
                if lead.user_id != old_user or lead.team_id != old_team:
                    lead._notify_assign_reassign(old_user)

        return result

    def unlink(self):
        if not self.env.su:
            raise AccessError(_("Deletion of leads is disabled for all roles. Please use the Archive Lead Wizard to archive leads."))
        return super(CrmLead, self).unlink()

    @api.model
    def search_panel_select_multi_range(self, field_name, **kwargs):
        """Core bug workaround: the web client's search panel sends group_domain=None
        (not omitted) for a select="multi" filter section with no group filters
        active yet. web.models.Base's own many2one branch does an unconditional
        AND([extra_domain, kwargs.get('group_domain', [])]) with no None-guard
        (unlike its many2many branch just above it, which checks 'if group_by and
        group_domain' first) - so a None here blows up in odoo.orm.domains.Domain()
        with TypeError: Domain() invalid argument type for domain: None. Only bites
        a many2one field used with select="multi" + groupby, which is exactly the
        Pipeline search panel's Salesperson section (views/crm_lead_views.xml -
        user_id is many2one, grouped under Sales Team) - hit locally 2026-09-04.
        Normalizing None to [] here, ahead of core, is the minimal fix.

        Must stay decorated @api.model, matching the original exactly - without
        it the RPC dispatcher's call_kw() no longer binds field_name at all
        (TypeError: missing 1 required positional argument: 'field_name'),
        also hit locally 2026-09-04.

        Also scopes the Salesperson section (field_name == 'user_id') to the
        CURRENT user's own team for everyone except CTO/Admin/MD/Corporate BU
        Manager - 2026-09-04: Salesperson was opened up to every login
        (previously CTO/Admin/MD only, same as Sales Team), so a DMT member
        must only ever see DMT members in that list, a Tech member only Tech
        members, etc., never the whole company. CTO/Admin/MD/Corporate BU
        Manager keep full cross-team visibility (already scoped by whichever
        team they pick via Sales Team's own groupby, x_mz_team_id on
        res.users). Corporate BU Manager added 2026-09-21 (client bug report:
        corp.mgr's Sales Team section was empty - see crm_lead_views.xml's
        matching groups= list on the searchpanel field, which must stay in
        sync with this check) - confirmed live they genuinely read across all
        5 Corporate sub-teams (Hunter/AM/Corp Training/LMS/TNH), the same
        shape of cross-team visibility as CTO/Admin/MD, just narrower in
        scope; without this, picking one of those 5 teams here would still
        wrongly scope the Salesperson list to corp.mgr's own tiny 3-person
        team_corporate instead of the team actually picked."""
        if kwargs.get('group_domain') is None:
            kwargs['group_domain'] = []
        if field_name == 'user_id':
            user = self.env.user
            if not (
                user.has_group('mazenet_access_rights.group_mzr_cto_admin')
                or user.has_group('mazenet_access_rights.group_mzr_md')
                or user.has_group('mazenet_access_rights.group_mzr_corporate_manager')
            ):
                own_team = self._mz_user_own_team(user)
                team_domain = [('id', 'in', own_team.member_ids.ids)] if own_team else [('id', '=', 0)]
                kwargs['comodel_domain'] = (kwargs.get('comodel_domain') or []) + team_domain
            else:
                # CTO/Admin/MD/Corporate BU Manager: the Salesperson section also carries groupby="x_mz_team_id"
                # (views/crm_lead_views.xml), which routes core's
                # search_panel_select_multi_range into its many2one+group_by branch - that
                # branch builds its value list purely from comodel_domain (all res.users
                # matching it) and only uses category_domain for the __count numbers, NOT
                # for pruning which users even appear. So selecting "Tally" in the Sales
                # Team category above had zero effect on which salespeople showed up here
                # (hit 2026-09-09). Pull the selected team id(s) straight out of the
                # category_domain leaf core already built for us and fold them into
                # comodel_domain. Can't filter via x_mz_team_id itself here - it's a
                # non-stored compute with no search() method, and comodel_domain gets
                # compiled straight to SQL (ValueError: Cannot convert
                # res.users.x_mz_team_id to SQL because it is not stored, hit
                # 2026-09-09) - so resolve the team(s) to their member_ids ourselves and
                # filter res.users by id instead.
                team_ids = []
                for leaf in (kwargs.get('category_domain') or []):
                    if isinstance(leaf, (list, tuple)) and len(leaf) == 3 and leaf[0] == 'team_id':
                        value = leaf[2]
                        team_ids += list(value) if isinstance(value, (list, tuple)) else [value]
                if team_ids:
                    member_ids = self.env['crm.team'].sudo().browse(team_ids).exists().member_ids.ids
                    kwargs['comodel_domain'] = (kwargs.get('comodel_domain') or []) + [
                        ('id', 'in', member_ids)
                    ]
        return super().search_panel_select_multi_range(field_name, **kwargs)

    @api.model
    def search_panel_select_range(self, field_name, **kwargs):
        """Hides the Corporate team from the Sales Team search panel section
        (category type - field_name == 'team_id') without touching the
        underlying crm.team record: not in use yet, needed again in a future
        phase. category sections don't accept a view-level domain= attribute
        (the JS parser only reads attrs.domain for select="multi" filter
        sections, confirmed in search_arch_parser.js's visitSearchPanel - a
        category section always calls search_panel_select_range, whose JS
        caller in search_model.js only ever sends category_domain, never
        comodel_domain), so this is the only place that exclusion can
        actually take effect."""
        if field_name == 'team_id':
            corporate = self.env.ref('mazenet_crm.team_corporate', raise_if_not_found=False)
            if corporate:
                kwargs['comodel_domain'] = (kwargs.get('comodel_domain') or []) + [('id', '!=', corporate.id)]
        return super().search_panel_select_range(field_name, **kwargs)

    # (Agent-tier, ATL-tier, Team Lead-tier, BU Manager-tier) group chains from
    # mazenet_access_rights, independent of crm.team. teams.xml consolidates Hunter/
    # Account Manager/Corporate Training/LMS/TNH into one team_corporate record, and
    # Tally's Development/Sales branches into one team_tally record - but the
    # access-rights GROUP hierarchy stays fully separate per sub-team regardless (e.g.
    # group_mzr_hunter_tl is not the same group as group_mzr_lms_tl). That means a
    # single crm.team can no longer be mapped to one fixed tier-group tuple, so the
    # "Team"/"Internal" assign-type gate (_compute_x_assignable_user_ids) resolves
    # tier from a user's actual group membership instead of from a crm.team: each
    # user can only belong to one of these chains, so checking which one they hold
    # gives an unambiguous answer regardless of how teams.xml groups crm.team
    # records. RED-lock release authority (can_user_release_lock) does NOT use this
    # any more (see its own docstring, 2026-09-21) - only _mz_user_tier_chain and
    # _mz_assign_notify_recipients still rely on it.
    MZR_TIER_GROUP_CHAINS = [
        ('group_mzr_dmt_agent', 'group_mzr_dmt_atl', 'group_mzr_dmt_tl', 'group_mzr_dmt_manager'),
        ('group_mzr_technology_agent', 'group_mzr_technology_atl', 'group_mzr_technology_tl', 'group_mzr_technology_manager'),
        ('group_mzr_software_agent', 'group_mzr_software_atl', 'group_mzr_software_tl', 'group_mzr_software_manager'),
        ('group_mzr_mis_agent', 'group_mzr_mis_atl', 'group_mzr_mis_tl', 'group_mzr_mis_manager'),
        ('group_mzr_hunter_agent', 'group_mzr_hunter_atl', 'group_mzr_hunter_tl', 'group_mzr_corporate_manager'),
        ('group_mzr_account_manager_agent', 'group_mzr_account_manager_atl', 'group_mzr_account_manager_tl', 'group_mzr_corporate_manager'),
        ('group_mzr_corporate_training_agent', 'group_mzr_corporate_training_atl', 'group_mzr_corporate_training_tl', 'group_mzr_corporate_manager'),
        ('group_mzr_lms_agent', 'group_mzr_lms_atl', 'group_mzr_lms_tl', 'group_mzr_corporate_manager'),
        ('group_mzr_tnh_agent', 'group_mzr_tnh_atl', 'group_mzr_tnh_tl', 'group_mzr_corporate_manager'),
        ('group_mzr_tally_atl_agents_dev', 'group_mzr_tally_atl_dev', 'group_mzr_tally_tl_development', 'group_tally_manager'),
        ('group_mzr_tally_atl_agents_sales', 'group_mzr_tally_atl_sales', 'group_mzr_tally_tl_sales', 'group_tally_manager'),
    ]
    MZR_TIER_RANK = {'agent': 0, 'atl': 1, 'tl': 2, 'manager': 3}

    @api.model
    def _mz_user_tier_chain(self, user):
        """('agent'|'atl'|'tl'|'manager', chain) for `user`, or (None, None) if they
        hold no recognized mazenet_access_rights role. `chain` is the matching 4-tuple
        from MZR_TIER_GROUP_CHAINS (checked highest tier first, since e.g. a Manager
        also holds the Agent group transitively via implied_ids)."""
        for chain in self.MZR_TIER_GROUP_CHAINS:
            agent_g, atl_g, tl_g, manager_g = chain
            if user.has_group(f'mazenet_access_rights.{manager_g}'):
                return ('manager', chain)
            if user.has_group(f'mazenet_access_rights.{tl_g}'):
                return ('tl', chain)
            if user.has_group(f'mazenet_access_rights.{atl_g}'):
                return ('atl', chain)
            if user.has_group(f'mazenet_access_rights.{agent_g}'):
                return ('agent', chain)
        return (None, None)

    def can_user_release_lock(self, target_user=None):
        """Per the release policy: CTO/Admin always can; otherwise whoever the lead's
        OWNER actually reports to, directly or transitively (res.users.x_reports_to_id) -
        a Manager's own lead is releasable by that Manager themselves (self-release)
        besides CTO/Admin, since a Manager has no one else above them in their own BU
        chain (they report to CTO, already covered by the check above).

        REPLACES the old group-based version (removed 2026-09-21, client instruction:
        "red lock also should be release hierarchy wise only" - a real bug report, not
        a feature request, same root cause as the Salesperson dropdown fix earlier the
        same day): _mz_release_head_group used to resolve a FLAT mazenet_access_rights
        group one tier above the owner (e.g. group_mzr_hunter_atl for an agent's lead),
        and has_group() against that shared group couldn't tell two same-tier peers'
        subordinates apart - Hunter's ATL-2 could release a RED lock on ATL-1's own
        agent's lead, since both ATLs hold the exact same flat group. Reusing
        _mz_reports_to_users here (built for that same fix) gives the correct, specific
        answer: only the owner's OWN chain of command, walked via the real reporting
        line this module now populates."""
        self.ensure_one()
        u = target_user or self.env.user
        owner = self.user_id
        if not owner:
            return True
        if self.env.su or u.has_group('mazenet_access_rights.group_mzr_cto_admin'):
            return True
        if owner == u:
            tier, _chain = self._mz_user_tier_chain(owner)
            return tier == 'manager'
        return owner in self._mz_reports_to_users(u)

    def _mz_can_edit_by_team(self, user):
        """Whether `user` currently qualifies for team-based edit access to this lead -
        besides CTO/Admin (checked separately by callers) and the lead's own owner
        (also checked separately - this method doesn't know or care who owns it),
        that's the CURRENT team_id's ATL/TL/Manager: a member of team_id.member_ids
        who holds at least ATL tier (_mz_user_tier_chain). Deliberately gated on
        team_id/member_ids rather than a fixed group chain (like
        can_user_release_lock/_mz_team_tier_groups use, keyed off the OWNER's own
        groups) - a fixed chain wouldn't change just because team_id does, which would
        defeat the point: the moment someone transfers this lead to a different team
        via the team_id field, whoever used to qualify here (the old team's ATL/TL/
        Manager) stops being a member of the NEW team_id and loses this access, same
        as everyone else not on that new team.

        Used by _mz_can_edit_owned for the UNLOCKED case only, for any NON-owner (a
        TL/ATL/Manager working a lead they don't personally own), OR'd with a
        separate, tier-agnostic membership check for the owner themselves. NOT used
        for the RED-locked case in write() - that's can_user_release_lock, which is
        keyed off the OWNER's specific head group (one tier above them) rather than
        "any ATL/TL/Manager on the team", so it excludes the locked owner even if
        they hold ATL/TL/Manager tier themselves, and excludes unrelated peers at
        that tier too.

        Corporate BU Manager (2026-09-22) counts as a member here too for their 5
        oversight teams specifically, even without a literal team_id.member_ids
        row - see _mz_is_corp_manager_oversight_team."""
        self.ensure_one()
        if not self.team_id or (
            user not in self.team_id.member_ids
            and not self._mz_is_corp_manager_oversight_team(user, self.team_id)
        ):
            return False
        tier, _chain = self._mz_user_tier_chain(user)
        return tier in ('atl', 'tl', 'manager')

    def _mz_can_edit_owned(self, user):
        """Whether `user` may edit this UNLOCKED lead's content - owned or not, name
        aside this is the general-purpose check for the non-locked case. Sales Team
        (team_id) is the single source of truth, no owner exemption - `user` must
        currently be a member of team_id.member_ids, full stop. Within that, two
        cases: the lead's OWNER (if any) may edit it at ANY tier (an Agent editing
        their own lead is normal day-to-day CRM use, not something this rule should
        block) as long as they're still on the team it's filed under; an UNOWNED
        lead is likewise editable by any tier on the team (nobody's turf to
        protect yet - includes the Agent who just cleared it themselves via
        Team/Internal assign-type mid-edit: x_content_readonly_for_me recomputes
        live off the in-progress user_id, and DMT+Team intentionally sets user_id
        to False before team_id has even changed, so without this the form went
        fully read-only the instant 'Team' was picked, before the Agent could
        even choose a team - hit live 2026-09-11); anyone else editing a lead
        SOMEONE ELSE owns additionally needs ATL/TL/Manager tier
        (_mz_can_edit_by_team). Either way, the moment team_id moves elsewhere,
        whoever isn't a member of the NEW team loses access - owner included -
        matching how the transfer itself only ever considers Sales Team, nothing
        else.

        DMT is NOT exempt from this (client instruction, 2026-09-11, reversing an
        earlier blanket bypass): a transferred lead greys out for DMT exactly like
        it does for every other team's team-transfer readonly, including the
        narrow "reassign salesperson only" waiver this used to preserve - team_id
        moving off DMT ends DMT's involvement entirely, full read-only, same as
        anyone else. RED-lock read-only (_mz_can_edit_by_team, used directly in
        write() while locked) is untouched by this either way.

        MD gets a narrower waiver, scoped to leads they personally own: MD isn't
        a member of any crm.team at all (by design - global read-only role), so
        without this they'd fail the team-membership check even on a lead they
        just created for themselves (write()'s own MD gate already restricts them
        to owned leads only, so this doesn't widen anything - it just lets that
        case reach here instead of dead-ending on team membership).

        Corporate BU Manager (2026-09-22, client instruction: corp.mgr "should
        have all access regarding pipeline and also edit access, bcoz under his
        supervision only those 5 teams will come") gets the same treatment as a
        literal team member for their 5 oversight teams - see
        _mz_is_corp_manager_oversight_team. Without this, every lead on Hunter/
        Account Manager/Corp Training Delivery/LMS/TNH showed corp.mgr the
        "transferred to another team, read-only" message, since they're only
        really enrolled in team_corporate's own member_ids."""
        self.ensure_one()
        if user == self.user_id and user.has_group('mazenet_access_rights.group_mzr_md'):
            return True
        if not self.team_id or (
            user not in self.team_id.member_ids
            and not self._mz_is_corp_manager_oversight_team(user, self.team_id)
        ):
            return False
        if user == self.user_id or not self.user_id:
            return True
        return self._mz_can_edit_by_team(user)

    def action_release_lock(self):
        """Clears the RED lock, making the lead editable again - only for whoever
        can_user_release_lock() authorizes (the owner's head/superior, or CTO/Admin)."""
        for lead in self:
            if not lead.can_user_release_lock():
                raise AccessError(_(
                    "You are not authorized to release the RED lock on lead '%s'. Only "
                    "the owner's Team Lead/Manager (or CTO/Admin) can release it."
                ) % lead.name)

            lead.write({'x_is_locked': False, 'x_lock_date': False})
            lead.message_post(body=_("RED lock released by %s. Lead is editable again.") % self.env.user.name)

            if lead.user_id:
                lead._push_notification(
                    lead.user_id,
                    subject=_("RED Lock Released"),
                    body=_("Lead '%s' has been released by %s and is editable again.") % (lead.name, self.env.user.name),
                )

    def action_set_lost(self, **additional_values):
        """Stock CRM's 'Mark Lost' flow (crm.lead.lost wizard -> here -> action_archive
        -> write({'active': False})) is a normal, everyday sales action open to
        whoever owns the lead - NOT the same thing as the Direct Lead Archiving
        Restriction in write() is guarding against (a raw active=False bypassing
        the CTO-only Archive Lead Wizard). Without this, every non-CTO/Admin user
        hit an AccessError just clicking Lost, since action_archive()'s write()
        never stamps mz_archive_wizard - only mazenet_crm's own wizard does.
        Stamping it here waives that gate for this one legitimate path, the same
        way the Archive Lead Wizard does for itself.

        Also covers a SECOND, now-removed action_set_lost override that used to
        also move a newly-Lost lead onto its BU's shared Won/Lost stage
        (motivated by a real complaint, 2026-09-15: a Tally lead marked Lost
        stayed on "New Lead" instead of showing under "Won / Lost" when
        browsing Archived leads). That code was dead (silently shadowed by
        this method - same class, same method name defined twice, only the
        later definition in the file ever ran) before being removed - testing
        it for the first time here (2026-09-19) showed WHY it can't work:
        stock's own _check_won_validity constraint
        (addons/crm/models/crm_lead.py:262-266) unconditionally forbids a lead
        sitting on an is_won=True stage with probability != 100 ("A lead in a
        Won stage cannot be lost. Move it to another stage first.") - and
        stock's write() ALSO unconditionally forces probability=100/active=True
        the instant stage_id targets an is_won stage, so there is no write
        ordering that lands a Lost lead on that stage without stock rejecting
        it or silently re-Won-ing it. This is a genuine limitation of the "Won
        and Lost share one is_won stage" design this module already uses for
        Tally, not something specific to Corporate - flagged to the user
        rather than worked around here, since fixing it for real needs a
        separate is_won=False "Lost" stage per BU, a bigger change than this
        task's scope. Lost leads stay on whatever stage they were on when
        closed, same as stock's own default behavior.

        Corporate/LMS/TNH's own free-text Lost Reason gate (x_lost_reason_text)
        was removed 2026-09-21 (client decision: use stock's default Lost
        functionality - the crm.lead.lost wizard's own lost_reason_id/feedback
        - for every BU, not a per-BU custom field)."""
        return super(
            CrmLead, self.with_context(mz_archive_wizard=True)
        ).action_set_lost(**additional_values)

    def action_view_spinoff_leads(self):
        """Smart-button target: leads created FROM this one via the 'Create New
        Lead' wizard (x_related_lead_id back-reference)."""
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('crm.crm_lead_all_leads')
        action['domain'] = [('x_related_lead_id', '=', self.id)]
        action['context'] = {}
        return action

        return True

    def _push_notification(self, users, subject, body):
        """Real-time + persistent notification: files an Inbox (needaction) message for each
        target user via mail's own notification pipeline. If they're online right now it
        pushes live over the bus (shows immediately, same as a popup); if they're not, it
        still sits in their Inbox/systray envelope the next time they log in - unlike a
        plain bus toast, which is lost entirely if nobody's there to see it."""
        self.ensure_one()
        partners = users.mapped('partner_id').filtered(lambda p: p)
        if not partners:
            return
        self.message_notify(
            partner_ids=partners.ids,
            subject=subject,
            body=body,
        )

    def _mz_assign_notify_recipients(self, new_owner):
        """CTO/Admin always, plus whichever of the lead's CURRENT team_id's own ATL/TL/
        Manager members sit strictly ABOVE new_owner's tier (client instruction,
        2026-09-09: notify 'CTO or Manager or TL or ATL' whenever a lead is assigned/
        reassigned) - e.g. an Agent getting assigned notifies their team's ATL, TL AND
        Manager; a lead reassigned straight to a TL only notifies that team's Manager
        (their ATL peers/subordinates aren't "above" them). If new_owner is empty (DMT+
        Team bulk hand-off deliberately leaves user_id unset) or holds no recognized
        tier, every ATL/TL/Manager member of the team is notified instead - there's no
        specific tier to be "above" yet. Scoped to THIS team only (team_id.member_ids),
        not company-wide, same reasoning as _mz_can_edit_by_team."""
        self.ensure_one()
        cto_group = self.env.ref('mazenet_access_rights.group_mzr_cto_admin', raise_if_not_found=False)
        # res.groups has no 'users' field in Odoo 19 - it's all_user_ids (also picks up
        # implied membership, e.g. Admin via a higher-level implied_ids chain), NOT the
        # non-existent 'users' attribute (AttributeError, hit live 2026-09-10).
        recipients = cto_group.all_user_ids if cto_group else self.env['res.users']
        if not self.team_id:
            return recipients
        owner_tier, _chain = self._mz_user_tier_chain(new_owner) if new_owner else (None, None)
        owner_rank = self.MZR_TIER_RANK.get(owner_tier, -1)
        for member in self.team_id.member_ids:
            tier, _chain = self._mz_user_tier_chain(member)
            if tier in ('atl', 'tl', 'manager') and self.MZR_TIER_RANK[tier] > owner_rank:
                recipients |= member
        return recipients

    def _notify_assign_reassign(self, old_owner):
        """Chatter + real-time/persistent Inbox notification whenever a lead's user_id
        or team_id actually changes (single-lead form, bulk Mass Assign wizard, or any
        other write() that touches either) - client instruction, 2026-09-09. Fires from
        write() itself so every path that can change assignment is covered without
        needing to duplicate this in each wizard/onchange."""
        self.ensure_one()
        new_owner_name = self.user_id.name if self.user_id else _("Unassigned")
        old_owner_name = old_owner.name if old_owner else _("Unassigned")
        body = _(
            "Lead reassigned: %(old)s → %(new)s (Team: %(team)s), by %(actor)s."
        ) % {
            'old': old_owner_name, 'new': new_owner_name,
            'team': self.team_id.name or _("None"), 'actor': self.env.user.name,
        }
        self.message_post(body=body, subtype_xmlid="mail.mt_note")

        recipients = self._mz_assign_notify_recipients(self.user_id) - self.env.user
        if recipients:
            self._push_notification(
                recipients,
                subject=_("Lead Assigned/Reassigned: %s") % self.name,
                body=body,
            )

    def _notify_red_lock_triggered(self):
        """Chatter (audit trail on the lead) + real-time/persistent Inbox notification +
        a standing activity for the lead's owner when a lock triggers."""
        self.ensure_one()
        owner = self.user_id
        owner_name = owner.name if owner else _("Unassigned")

        self.message_post(
            body=_("RED LOCK triggered: lead is overdue and now read-only (owner: %s).") % owner_name,
            subtype_xmlid="mail.mt_note",
        )

        if owner:
            self._push_notification(
                owner,
                subject=_("RED Lock: Action Required"),
                body=_("Lead '%s' is RED-locked and needs 'Release RED Lock' before it can be edited again.") % self.name,
            )

            self.activity_schedule(
                'mail.mail_activity_data_todo',
                summary=_("Release RED Lock: %s") % self.name,
                note=_("This lead is overdue and RED-locked. Review it and use 'Release RED Lock' to make it editable again."),
                user_id=owner.id,
            )

    @api.model
    def _cron_trigger_red_locks(self):
        """Auto-trigger the RED lock on leads whose next activity's real moment
        (x_next_activity_datetime - already resolved per-activity-type, see mail_activity.py)
        is more than MZ_ACTIVITY_WINDOW_MINUTES (20) in the past. A 10:00 AM activity locks
        at 10:20, not the instant 10:00 passes - gives the owner a short window to still
        make it before it counts against them. Scoped to MZ_ACTIVITY_CARD_BU_CATEGORIES
        (DMT/Tally/Technology) - client rework spec (2026-09-08): Software Dev and MIS have
        no Follow-up's stage, so the RED lock itself no longer applies there, not just its
        colour. data/cron.xml calls check_red_lock_recods() (right below) immediately after
        this, in the same cron tick, and that method no longer waits out a second grace
        period of its own before escalating to the Team Lead - the client wants the TL
        notified together with RED triggering, not some extra minutes after that."""
        now = fields.Datetime.now()
        cutoff = now - timedelta(minutes=MZ_ACTIVITY_WINDOW_MINUTES)

        leads = self.sudo().search([
            ('x_next_activity_datetime', '!=', False),
            ('x_next_activity_datetime', '<', cutoff),
            ('x_is_locked', '=', False),
            ('active', '=', True),
            ('user_id', '!=', False),
            ('team_id.x_bu_category', 'in', list(MZ_ACTIVITY_CARD_BU_CATEGORIES)),
        ])
        for lead in leads:
            lead.write({
                'x_is_locked': True,
                'x_lock_date': now,
            })
            lead._notify_red_lock_triggered()

    @api.model
    def _cron_lms_friday_escalation(self):
        """LMS Pipeline Stage 7 escalation (Build Notes #6 - the project's ONLY
        cron): if the upcoming training week's KT or Skill upload isn't checked yet,
        notify the LMS team leader (the sheet's open "who gets notified" question -
        built with the LMS Manager, i.e. team_lms.user_id, as the proposed default).

        data/cron.xml runs this DAILY, nextcall pinned to 20:00 IST (14:30 UTC) -
        deliberately not a weekly interval anchored to a guessed Friday date, since
        the sheet only requires the run TIME be stated explicitly in IST, not the
        underlying poll cadence. The method itself is the actual "Friday night" gate:
        it no-ops on every other day, checked in IST (not server/UTC time - a
        server-time Friday can already be Saturday morning in IST or vice versa).

        "Next week" = whichever week's start date (training start + 7*(n-1) days)
        falls within the next 7 days from today - i.e. the week that starts on or
        before next Friday, checked from THIS Friday. Each lead gets at most one
        escalation per run, for that one upcoming week only."""
        ist = pytz.timezone('Asia/Kolkata')
        now_ist = pytz.utc.localize(fields.Datetime.now()).astimezone(ist)
        if now_ist.weekday() != 4:  # Monday=0 ... Friday=4
            return

        lms_team = self.env.ref('mazenet_crm.team_lms', raise_if_not_found=False)
        if not lms_team:
            return
        today = now_ist.date()
        leads = self.search([
            ('team_id', '=', lms_team.id),
            ('x_lms_training_start_date', '!=', False),
            ('x_lms_week_ids', '!=', False),
        ])
        for lead in leads:
            for week in lead.x_lms_week_ids.sorted('week_number'):
                week_start = lead.x_lms_training_start_date + timedelta(days=7 * (week.week_number - 1))
                if today <= week_start <= today + timedelta(days=7):
                    if not week.kt_uploaded or not week.skill_uploaded:
                        lead._notify_lms_week_escalation(week, lms_team.user_id)
                    break

    def _notify_lms_week_escalation(self, week, recipient):
        """Chatter + real-time/persistent Inbox notification for one unchecked
        upcoming LMS training week - see _cron_lms_friday_escalation."""
        self.ensure_one()
        missing = []
        if not week.kt_uploaded:
            missing.append(_("KT"))
        if not week.skill_uploaded:
            missing.append(_("Skill"))
        body = _(
            "LMS Friday Escalation: Week %(week)s upload(s) still unchecked: %(missing)s."
        ) % {'week': week.week_number, 'missing': ', '.join(missing)}
        self.message_post(body=body, subtype_xmlid="mail.mt_note")
        if recipient:
            self._push_notification(
                recipient,
                subject=_("LMS Week %s Upload Escalation: %s") % (week.week_number, self.name),
                body=body,
            )

    def _get_parent_hierarchy(self, group):
            """Recursively fetch all parent/ancestor groups."""
            parents = self.env['res.groups'].search([('implied_ids', 'in', group.id)])
            for parent in parents:
                parents |= self._get_parent_hierarchy(parent)
            return parents

    def check_red_lock_recods(self):
        """Escalate a RED lock to the owner's Team Lead/Manager chain. Runs on every
        cron tick (data/cron.xml, right after _cron_trigger_red_locks) against every
        currently-locked lead in scope - no elapsed-since-lock delay of its own anymore
        (client rework spec, 2026-09-08: the TL should be notified together with RED
        triggering, not some extra minutes after that - this used to wait out a SECOND
        grace_time on top of the one that already delayed the lock itself, so a TL
        wasn't actually notified until ~2x the intended grace period had passed).
        Idempotent via the existing_activity check below, so running it on every
        already-escalated lead every 2 minutes is harmless. Scoped to
        MZ_ACTIVITY_CARD_BU_CATEGORIES the same as the lock itself."""
        red_lock_rec_vals = self.search([
            ('x_is_locked', '=', True),
            ('team_id.x_bu_category', 'in', list(MZ_ACTIVITY_CARD_BU_CATEGORIES)),
        ])
        todo_activity_type = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        activity_type_id = todo_activity_type.id if todo_activity_type else False
        for lead in red_lock_rec_vals:
            user = lead.user_id
            if not user:
                continue
            target_groups = lead.team_id.privelege_ids.mapped('group_ids')
            matching_groups = target_groups & user.group_ids
            parent_users = self.env['res.users']
            for group in matching_groups:
                all_parents = self._get_parent_hierarchy(group)
                for parent in all_parents:
                    parent_users |= parent.user_ids
            escalation_users = parent_users - user
            for parent_user in escalation_users:
                existing_activity = self.env['mail.activity'].sudo().search([
                    ('res_model', '=', 'crm.lead'),
                    ('res_id', '=', lead.id),
                    ('user_id', '=', parent_user.id),
                    ('summary', '=', 'Red Lock Release Pending'),
                ], limit=1)
                if not existing_activity:
                    lead.activity_schedule(
                        activity_type_id=activity_type_id,
                        summary="Red Lock Release Pending",
                        note=(
                            f"<p><strong>Alert:</strong> No one has released the Red Lock on lead "
                            f"<strong>{lead.name}</strong> assigned to <strong>{user.name}</strong>.</p>"
                            f"<p>Overdue by more than {MZ_ACTIVITY_WINDOW_MINUTES} minutes.</p>"
                        ),
                        user_id=parent_user.id,
                        date_deadline=fields.Date.context_today(self),)



    _MZ_TEAM_LEAD_POOLS = {
        'mazenet_crm.team_dmt': (
            ["Rajesh Traders", "Sunrise Textiles", "Om Sai Enterprises", "Kaveri Foods Pvt Ltd",
             "Shree Balaji Hardware", "New Bharat Stationers", "Ganpati Agro Foods", "Vinayak Plastics"],
            "Inquiry - %s", 15000,
        ),
        'mazenet_crm.team_tally': (
            ["Sharma & Sons Traders", "Golden Textiles Mills", "Anand Auto Spares", "Krishna Rice Mill",
             "Vishal Electricals", "Om Enterprises", "Patel Hardware Store", "Laxmi Garments"],
            "Tally Deal - %s", 25000,
        ),
        'mazenet_crm.team_corporate': (
            ["Meridian Logistics Pvt Ltd", "Zenith Manufacturing Corp", "Apex Infrastructure Ltd", "Orion Retail Chain",
             "Falcon Energy Solutions", "Skyline Constructions", "Prime Steel Industries", "Coastal Shipping Corp",
             "Bright Future Public School", "Global Institute of Technology", "Sunrise Degree College",
             "National Skill Academy", "Everest Public School", "Coastal Management Institute",
             "Blue Orchid Resorts", "Grand Palace Hotels", "Coastal Getaway Resorts", "Heritage Inn Group",
             "Emerald Beach Resort", "Silver Sands Hotel"],
            "Corporate Deal - %s", 90000,
        ),
        'mazenet_crm.team_technology': (
            ["NextGen Solutions", "Skyline Systems", "Vertex Apps", "Quantum Labs",
             "Bluewave Technologies", "Ironclad Networks"],
            "Tech Project - %s", 90000,
        ),
        'mazenet_crm.team_software': (
            ["Om Industries", "Shree Traders", "Metro Retail", "Apex Corp",
             "Vertex Pharma", "Nova Logistics"],
            "Custom Dev - %s", 120000,
        ),
        'mazenet_crm.team_mis': (
            ["City Hospital", "Coastal Bank", "Apex University", "Metro Retail Group",
             "Horizon Insurance", "Unity Financial Services"],
            "MIS Request - %s", 40000,
        ),
    }

    @api.model
    def _mz_seed_business_leads(self, leads_per_user=4):
        """Demo-data generator: gives every @test.mazenet user (except CTO/MD, who are
        read-only demo accounts and don't own leads) `leads_per_user` business-appropriate
        leads, spread across their own team's real stages. Idempotent - re-running this
        (it's called from demo_data.xml on every install/update) tops a user up to the
        target count rather than creating duplicates on top of what they already have."""
        CrmLead = self.env['crm.lead'].sudo()
        ResUsers = self.env['res.users'].sudo()
        CrmStage = self.env['crm.stage'].sudo()

        pools = {}
        for xmlid, (companies, template, base_revenue) in self._MZ_TEAM_LEAD_POOLS.items():
            team = self.env.ref(xmlid, raise_if_not_found=False)
            if team:
                pools[team.id] = {
                    'companies': companies, 'template': template,
                    'base_revenue': base_revenue, 'counter': 0,
                }

        excluded_logins = {'cto@test.mazenet', 'md@test.mazenet'}
        users = ResUsers.search([('login', '=like', '%@test.mazenet')]).filtered(
            lambda u: u.login not in excluded_logins
        )

        to_create = []
        for user in users:
            existing_count = CrmLead.search_count([('user_id', '=', user.id)])
            if existing_count >= leads_per_user:
                continue

            teams = user.crm_team_ids.filtered(lambda t: t.id in pools)
            if not teams:
                continue

            for i in range(leads_per_user - existing_count):
                # A Corp BU Manager spans all 4 Corp teams - rotate their leads across them.
                # Everyone else only has one team, so this is just teams[0] every time.
                team = teams[i % len(teams)]
                data = pools[team.id]
                company = data['companies'][data['counter'] % len(data['companies'])]
                data['counter'] += 1

                stages = CrmStage.search([('team_ids', 'in', [team.id])], order='sequence asc')
                if not stages:
                    continue
                slot_index = [0, len(stages) // 3, (2 * len(stages)) // 3, len(stages) - 1][i % 4]
                stage = stages[slot_index]

                to_create.append({
                    'name': data['template'] % company,
                    'partner_name': company,
                    'team_id': team.id,
                    'stage_id': stage.id,
                    'user_id': user.id,
                    'expected_revenue': data['base_revenue'] + (i * 5000) + (existing_count * 1000),
                })

        if to_create:
            CrmLead.create(to_create)
        return len(to_create)
