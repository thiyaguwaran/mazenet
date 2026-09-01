# -*- coding: utf-8 -*-
import re
from datetime import timedelta

from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError

MZ_PHONE_RE = re.compile(r'^\d{10}$')
MZ_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

# Build-notes format validation ("Format validation only: valid 10-digit number" /
# "valid email syntax... No dummy-number/dummy-email detection") is the same rule
# repeated verbatim across all 5 M2 pipeline sheets - scoped to just those BUs so
# other teams (Corporate/LMS/TNH/Hunter...) aren't newly constrained by this build.
MZ_FORMAT_VALIDATED_BU_CATEGORIES = {'dmt', 'tally', 'tech', 'swdev', 'mis'}

# Grace period between an activity's real due moment (x_next_activity_datetime) and the
# RED lock actually triggering - e.g. a 10:00 AM activity locks at 10:15, not the instant
# 10:00 passes.
MZ_RED_LOCK_GRACE_MINUTES = 15

# x_assign_type drives user_id alongside team_id (assign_salesperson onchange) - it's
# part of the same reassignment picker, not lead "content", so it belongs here too:
# without it, a BU Manager doing a normal Self/Team/Internal team hand-off through the
# form (which always touches x_assign_type together with team_id/user_id) would trip
# the "Managers view and reassign; content edits go through the Team Lead" rule below
# on a lead they don't own, even though nothing about the lead's actual content changed.
REASSIGN_FIELDS = {"user_id", "team_id", "stage_id", "x_assign_type"}
SYSTEM_FIELDS = {
    "message_follower_ids", "activity_ids", "message_ids", "message_main_attachment_id",
    "website_message_ids", "message_has_error", "message_has_error_counter", "message_needaction",
    "message_needaction_counter", "message_is_follower", "message_partner_ids", "activity_state",
    "activity_user_id", "activity_type_id", "activity_date_deadline", "activity_summary",
    "activity_exception_type", "activity_exception_decoration", "active",
    "x_is_locked", "x_lock_date",
}

# The 6 BU Manager-tier groups from mazenet_access_rights. Kept as an explicit list (rather
# than a naming-pattern match) since the module isolates each team's groups from the others
# on purpose - there's no single "any manager" group to check against.
MZR_MANAGER_GROUPS = [
    "mazenet_access_rights.group_mzr_dmt_manager",
    "mazenet_access_rights.group_mzr_technology_manager",
    "mazenet_access_rights.group_mzr_software_manager",
    "mazenet_access_rights.group_mzr_mis_manager",
    "mazenet_access_rights.group_mzr_corporate_manager",
    "mazenet_access_rights.group_tally_manager",
]

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
MZ_STAGE_GATE_RULES = {
    'dmt': [
        ('stage_dmt_new', ['name', 'phone', 'email_from', 'x_organic_inorganic', 'source_id']),
        ('stage_dmt_contacted', [
            'x_company_or_individual', 'x_contact_purpose', 'x_product_service',
            'x_employee_count', 'x_company_turnover',
        ]),
        ('stage_dmt_qualified', ['x_target_team_id', 'x_transfer_notes']),
        ('stage_dmt_transferred', []),
    ],
    'tally': [
        # Email is "progressive" here (not mandatory at Stage 1, mandatory from Stage 2
        # onward) - so it's checked leaving Stage 2, not Stage 1.
        ('stage_tally_new', ['name', 'phone', 'source_id']),
        ('stage_tally_contacted', ['x_tally_category', 'email_from']),
        ('stage_tally_demo', ['x_requirements_attachment', 'x_product_service', 'x_feasibility', 'x_timeline']),
        ('stage_tally_proposal', ['x_quote_date', 'x_quote_document']),
        ('stage_tally_negotiation', []),
        ('stage_tally_won', []),
        ('stage_tally_lost', []),
    ],
    'tech': [
        ('stage_tech_new', ['name', 'phone', 'email_from', 'source_id']),
        ('stage_tech_2', ['x_customer_status', 'x_customer_need']),
        ('stage_tech_3', [
            'x_requirements_attachment', 'x_product_service', 'x_feasibility_identified', 'x_timeline',
        ]),
        ('stage_tech_4', [
            'x_quote_shared_checkbox', 'x_customer_goods_finalised', 'x_quote_date', 'x_quote_document',
        ]),
        ('stage_tech_5', []),
        ('stage_tech_6', []),
        ('stage_tech_won', []),
    ],
    'swdev': [
        ('stage_swdev_new', ['name', 'phone', 'email_from', 'source_id']),
        ('stage_swdev_2', [
            'x_branch_count', 'x_sw_employee_count', 'x_nature_of_business',
            'x_established_year', 'x_meeting_attendees',
        ]),
        ('stage_swdev_3', ['x_demo_completed', 'x_system_study_attachment', 'x_feasibility', 'x_timeline']),
        ('stage_swdev_4', ['x_quote_date', 'x_quote_document']),
        ('stage_swdev_5', []),
        ('stage_swdev_won', []),
    ],
    'mis': [
        ('stage_mis_new', ['name', 'phone', 'email_from', 'source_id']),
        ('stage_mis_2', [
            'x_requirements_attachment', 'x_product_service', 'x_target_audience',
            'x_mis_timelines_estimate', 'x_deliverables',
        ]),
        ('stage_mis_3', ['x_demo_completed', 'x_system_study_attachment', 'x_feasibility', 'x_timeline']),
        ('stage_mis_4', ['x_quote_document']),
        ('stage_mis_5', []),
        ('stage_mis_won', []),
    ],
}

class CrmLead(models.Model):
    _inherit = "crm.lead"
    _order = "x_next_activity_datetime asc, priority desc, id desc"

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
             "the default Kanban/List ordering (soonest activity on top) instead of "
             "crm.lead's stock priority/id order; leads with no open activity naturally "
             "sort to the bottom (NULL last)."
    )

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
    def _mz_user_is_dmt(self, user=None):
        """Whether `user` (default: current user) is a direct member of the DMT team
        (_mz_user_own_team). Shared by every DMT waiver in this file - the assignable-
        pool compute, the create()/write() pool backstop (_mz_check_assign_type_allowed),
        and the write() RED-lock/team-transfer gate - so they can't drift out of sync."""
        user = user or self.env.user
        dmt_team = self.env.ref('mazenet_crm.team_dmt', raise_if_not_found=False)
        return bool(dmt_team) and self._mz_user_own_team(user) == dmt_team

    @api.model
    def _mz_default_team_id(self):
        """Default a new lead's Sales Team to the CREATING user's own team
        (_mz_user_own_team) - without this, a new lead's team_id falls back to
        whatever stock CRM's own _get_default_team_id resolves, which isn't
        guaranteed to match the actual creator's team. Since write() now strictly
        requires being a member of a lead's CURRENT team_id to touch it at all
        (see _mz_can_edit_owned), a wrong default here means hitting an
        AccessError on the very first save - this is what actually prevents
        that, for both the full form and the Kanban quick-create
        (crm.quick_create_opportunity_form, inherited to show team_id)."""
        return self._mz_user_own_team().id

    def _get_team_id_domain(self):
        return [("id", "not in", self.env.user.crm_team_ids.ids)]


    team_id = fields.Many2one(
        "crm.team",
        default=_mz_default_team_id,
        domain=_get_team_id_domain,)
        
    @api.model
    def _default_x_assign_type(self):
        """Agents can't use 'team' or 'internal' (see x_can_assign_beyond_self), so
        defaulting everyone to 'team' meant every Agent got bounced back to 'self'
        with a warning on every single new lead. Pick the default from the current
        user's own tier instead, so an Agent starts on 'self' - the only option
        that was ever going to stick for them - and TL/ATL/Manager keep the
        original 'team' default."""
        tier, _chain = self._mz_user_tier_chain(self.env.user)
        return 'team' if tier in ('atl', 'tl', 'manager') else 'self'

    x_assign_type = fields.Selection(
        [('self', 'Self'), ('team', 'Team'), ('internal', 'Internal')],
        string="Assign Type", default=_default_x_assign_type,
        help="How user_id gets populated:\n"
             "- Self: always the current user. Available to everyone.\n"
             "- Team: hand-picked from the selected team's 'Create To' users.\n"
             "- Internal: hand-picked from whoever's directly in a group ranked below "
             "yours in the team's configured privilege/group hierarchy (DMT: any team "
             "member, no restriction).\n"
             "'Team' and 'Internal' are only offered to Team Leads, ATLs and BU Managers "
             "(mazenet_access_rights) - an Agent has no one to delegate to, so both are "
             "restricted to Self for them."
    )
    x_can_assign_beyond_self = fields.Boolean(
        compute='_compute_x_assignable_user_ids',
        string="Can Assign Beyond Self",
        help="Whether the CURRENT user (the one viewing/editing this lead right now) "
             "holds at least ATL tier in mazenet_access_rights, and so may use the "
             "'Team' or 'Internal' assign types (Agents are restricted to 'Self'). Not "
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
    x_is_dmt_user = fields.Boolean(
        compute='_compute_x_assignable_user_ids',
        string="Is DMT User",
        help="Whether the CURRENT user (viewing/editing this lead right now) belongs "
             "to the DMT team - used in the view to exempt user_id from the general "
             "x_content_readonly_for_me gate for DMT, on top of the unrestricted "
             "assignment pool above. Not stored - reflects whoever has the form open."
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
             "open, same pattern as x_is_dmt_user."
    )

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

    @api.depends('team_id', 'x_assign_type')
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
        tier, _chain = self._mz_user_tier_chain(user)
        can_beyond_self = user_is_dmt or tier in ('atl', 'tl', 'manager')
        for lead in self:
            lead.x_is_dmt_user = user_is_dmt
            lead.x_can_assign_beyond_self = can_beyond_self
            if not can_beyond_self:
                lead.x_assignable_user_ids = False
            elif user_is_dmt:
                lead.x_assignable_user_ids = lead.team_id.member_ids
            elif lead.x_assign_type == 'team':
                lead.x_assignable_user_ids = lead.team_id.create_lead_id
            else:
                lead.x_assignable_user_ids = self._mz_team_subordinate_group_users(lead.team_id, user)

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
            self.user_id = self.team_id.create_lead_id and self.team_id.create_lead_id[0] or False
        if self.x_assign_type == 'internal':
            self.team_id = user.crm_team_ids[0]
            return
        if not self.x_can_assign_beyond_self:
            self.x_assign_type = 'self'
            self.user_id = user
            return {'warning': {
                'title': _("Assignment restricted"),
                'message': _("Only Team Leads, ATLs and BU Managers can assign to a team "
                            "or assign internally. Agents can only assign to themselves."),
            }}
        if self.user_id not in self.x_assignable_user_ids:
            self.user_id = False

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
             "because of the Sales Team rule (_mz_can_edit_owned) - NOT because of a "
             "RED lock. Kept separate from x_content_readonly_for_me (which covers "
             "both) so the UI can color-code the two causes differently: RED lock "
             "already gets red (ribbon/tint/banner) elsewhere, this one drives a "
             "grey tint/banner instead, in the kanban card and the form, so a "
             "lead that's simply moved off your team doesn't read as 'overdue' when "
             "it isn't. Not stored - same per-user reasoning as "
             "x_content_readonly_for_me."
    )

    @api.depends('x_is_locked', 'x_content_readonly_for_me')
    def _compute_x_team_transfer_readonly(self):
        for lead in self:
            lead.x_team_transfer_readonly = (
                lead.x_content_readonly_for_me and not lead.x_is_locked
            )

    x_activity_warning = fields.Selection(
        [
            ('purple', 'Activity due within 30 minutes'),
            ('orange', 'Activity due within 10 minutes'),
        ],
        string="Activity Warning",
        compute="_compute_x_activity_warning",
        help="Kanban card pre-warning before RED lock: purple in the 30-minute window "
             "before the next activity, orange in the 10-minute window - only shown to the "
             "lead's owner or, for a Meeting activity, one of its calendar attendees, not "
             "to everyone browsing the Pipeline. Deliberately not stored: this is inherently "
             "relative to 'now' and to the viewing user, so it's recomputed fresh on every "
             "read rather than cron-maintained, the same way Odoo's own activity_state is."
    )

    @api.depends('x_next_activity_datetime', 'x_is_locked')
    def _compute_x_activity_warning(self):
        now = fields.Datetime.now()
        for lead in self:
            lead.x_activity_warning = False
            if lead.x_is_locked or not lead.x_next_activity_datetime:
                continue
            if not lead._mz_user_involved_in_next_activity():
                continue
            minutes_to_go = (lead.x_next_activity_datetime - now).total_seconds() / 60
            if 0 <= minutes_to_go <= 10:
                lead.x_activity_warning = 'orange'
            elif 10 < minutes_to_go <= 30:
                lead.x_activity_warning = 'purple'

    def _mz_user_involved_in_next_activity(self):
        """Whether the current user should see this lead's pre-RED-lock warning: the lead's
        own owner always does; for a Meeting activity, so does anyone in its calendar
        attendee list (e.g. a TL sitting in on an agent's call) - Calls/To-Dos have no
        attendee list, so only the owner sees the warning for those."""
        self.ensure_one()
        u = self.env.user
        if self.user_id == u:
            return True
        open_activities = self.activity_ids.filtered('active')
        if not open_activities:
            return False
        next_activity = min(
            open_activities,
            key=lambda a: a._mz_resolve_activity_datetime() or fields.Datetime.now(),
        )
        return bool(
            next_activity.calendar_event_id
            and u.partner_id in next_activity.calendar_event_id.partner_ids
        )

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
    x_product_service = fields.Char(string="Product / Service")
    x_feasibility = fields.Char(string="Feasibility")
    x_timeline = fields.Char(string="Timeline")
    x_requirements_attachment = fields.Binary(string="Requirements Attachment", attachment=True)
    x_requirements_attachment_filename = fields.Char(string="Requirements Attachment Filename")
    x_quote_date = fields.Date(string="Quote Date")
    x_quote_document = fields.Binary(string="Quote Document", attachment=True)
    x_quote_document_filename = fields.Char(string="Quote Document Filename")
    x_company_turnover = fields.Monetary(string="Company Turnover", currency_field='company_currency')
    x_project_start_date = fields.Date(string="Project Start Date")
    x_project_start_attachment = fields.Binary(string="Project Start Attachment", attachment=True)
    x_project_start_attachment_filename = fields.Char(string="Project Start Attachment Filename")
    x_project_completed_date = fields.Date(string="Project Completed Date")
    x_project_completed_attachment = fields.Binary(string="Project Completed Attachment", attachment=True)
    x_project_completed_attachment_filename = fields.Char(string="Project Completed Attachment Filename")
    x_workorder_completion_date = fields.Date(
        string="Workorder Completion Date",
        help="MANUAL entry only - do not build an auto-fetch or any integration with the "
             "Work Order app."
    )
    x_deviation_days = fields.Integer(
        string="Deviation Days", compute="_compute_x_deviation_days", store=True,
        help="Auto-calculated from Project Completed Date vs Workorder Completion Date."
    )
    x_demo_completed = fields.Boolean(string="Demo / POC Completed")
    x_system_study_attachment = fields.Binary(string="System Study (PDF)", attachment=True)
    x_system_study_attachment_filename = fields.Char(string="System Study Filename")

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
            if lead.phone:
                cleaned = re.sub(r'[\s\-().]', '', lead.phone)
                if not MZ_PHONE_RE.fullmatch(cleaned):
                    raise ValidationError(_(
                        "'%(lead)s': Contact Number must be a valid 10-digit number "
                        "(got '%(value)s')."
                    ) % {'lead': lead.name, 'value': lead.phone})
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
        help="The BU this DMT lead is being transferred to."
    )
    x_transfer_notes = fields.Text(string="Transfer Notes / Reason")

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
    x_company_intro_done = fields.Boolean(string="Company Intro")

    # -- Technology only --
    x_customer_status = fields.Char(string="Customer Status")
    x_customer_need = fields.Char(string="Customer Need")
    x_feasibility_identified = fields.Boolean(string="Feasibility Evaluation Identified")
    x_bom_attachment = fields.Binary(string="BOM Received", attachment=True)
    x_bom_attachment_filename = fields.Char(string="BOM Filename")
    x_boq_attachment = fields.Binary(string="BOQ Received", attachment=True)
    x_boq_attachment_filename = fields.Char(string="BOQ Filename")
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

    def _mz_check_assign_type_allowed(self, vals):
        """Server-side backstop for x_assign_type in ('team', 'internal'): the view
        only offers those to ATL/TL/Manager tier (x_can_assign_beyond_self), and the
        onchange bounces an Agent back to 'self' and clears user_id if it falls outside
        x_assignable_user_ids - but all of that is UI-only, so a direct RPC/API write
        could still set either. Raises the same way the UI would have refused, instead
        of silently accepting it. Mirrors _compute_x_assignable_user_ids' DMT waiver
        (a DMT team member skips the tier gate entirely and gets the full team
        roster as their pool for both 'team' and 'internal') and its 'internal'
        pool for everyone else (_mz_team_subordinate_group_users) - keep all three
        in sync, or a UI selection could get rejected on save."""
        assign_type = vals.get('x_assign_type')
        if assign_type not in ('team', 'internal') or self.env.su:
            return
        user = self.env.user
        user_is_dmt = self._mz_user_is_dmt(user)
        tier, _chain = self._mz_user_tier_chain(user)
        if not user_is_dmt and tier not in ('atl', 'tl', 'manager'):
            raise AccessError(_(
                "Only Team Leads, ATLs and BU Managers can assign to a team or assign "
                "internally. Agents can only assign to themselves."))

        if not vals.get('user_id'):
            return
        # Mirrors _compute_x_assignable_user_ids' pool for 'team'/'internal': records
        # being written each keep their own team_id unless vals overrides it; create()
        # calls this before any record exists, so there's nothing to fall back to but
        # vals itself.
        for record in (self or [self.env['crm.lead']]):
            team_id = vals['team_id'] if 'team_id' in vals else (record.team_id.id if record else False)
            team = self.env['crm.team'].browse(team_id) if team_id else self.env['crm.team']
            if user_is_dmt:
                pool_ids = team.member_ids.ids
            elif assign_type == 'team':
                pool_ids = team.create_lead_id.ids
            else:
                pool_ids = self._mz_team_subordinate_group_users(team, user).ids
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

    def _mz_missing_mandatory_fields(self, field_names, vals):
        """Names of fields in `field_names` that are still empty, considering `vals`
        (what's being written in this same call) over the record's current stored value.
        Special-cased for 'source_id': when the (about-to-be-set) source requires a
        companion reference text (utm.source.x_requires_reference_text - Referral/Ads/
        GeM Bid style sources), 'referred' must be filled too even though it isn't its
        own entry in MZ_STAGE_GATE_RULES (it's conditional on the source, not always
        mandatory)."""
        self.ensure_one()
        missing = []
        for fname in field_names:
            value = vals[fname] if fname in vals else self[fname]
            if not value:
                missing.append(fname)
        if 'source_id' in field_names:
            source_id = vals['source_id'] if 'source_id' in vals else self.source_id.id
            if source_id and self.env['utm.source'].browse(source_id).x_requires_reference_text:
                referred = vals['referred'] if 'referred' in vals else self.referred
                if not referred:
                    missing.append('referred')
        return missing

    def _mz_stage_gate_check(self, new_stage, vals):
        """Raise UserError if moving to `new_stage` skips past a stage (in this lead's
        BU) whose own mandatory fields (MZ_STAGE_GATE_RULES) aren't filled yet - "Build
        the fields in M2, enforce the Mandatory column on stage change in M3" from the
        pipeline sheets. Only a FORWARD move (to a later stage) is gated; moving
        backward never is. Jumping straight past several stages checks every stage in
        between, not just the one immediately before `new_stage`."""
        self.ensure_one()
        team = self.team_id
        if not team or not new_stage:
            return
        resolved = self._mz_stage_gate_rules_resolved(team.x_bu_category)
        if not resolved:
            return
        stage_ids = [s.id for s, _fields in resolved]
        if new_stage.id not in stage_ids:
            return
        new_index = stage_ids.index(new_stage.id)
        current_stage = self.stage_id
        current_index = stage_ids.index(current_stage.id) if current_stage.id in stage_ids else -1
        if new_index <= current_index:
            return

        problems = []
        for stage, field_names in resolved[max(current_index, 0):new_index]:
            missing = self._mz_missing_mandatory_fields(field_names, vals)
            if missing:
                labels = ', '.join(self._fields[f].string for f in missing)
                problems.append(f"{stage.name}: {labels}")
        if problems:
            raise UserError(_(
                "'%(lead)s' can't move to '%(target)s' yet - required fields are still "
                "empty:\n%(details)s"
            ) % {'lead': self.name, 'target': new_stage.name, 'details': '\n'.join(problems)})

    @api.model_create_multi
    def create(self, vals_list):
        u = self.env.user
        if not self.env.su and u.has_group("mazenet_access_rights.group_mzr_md"):
            raise AccessError(_("MD role is read-only across all CRM models and cannot create leads."))
        for vals in vals_list:
            self._mz_check_assign_type_allowed(vals)
        return super(CrmLead, self).create(vals_list)

    def write(self, vals):
        u = self.env.user
        is_cto_admin = u.has_group("mazenet_access_rights.group_mzr_cto_admin")
        self._mz_check_assign_type_allowed(vals)

        # Direct Lead Archiving Restriction: leads are archived only via the Archive Lead
        # Wizard (which stamps mz_archive_wizard on the context), never a raw active=False.
        if "active" in vals and not vals["active"]:
            if not self.env.su and not is_cto_admin and not self.env.context.get("mz_archive_wizard"):
                raise AccessError(_("Leads can only be archived by CTO / Admin via the Archive Lead Wizard."))

        if not self.env.su and not is_cto_admin:
            # MD Read-Only Restriction
            if u.has_group("mazenet_access_rights.group_mzr_md"):
                raise AccessError(_("MD role is read-only across all CRM models and cannot edit leads."))

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
            # DMT Reassignment Waiver: mirrors x_is_dmt_user's view-level exemption of
            # user_id from x_content_readonly_for_me - a DMT team member may reassign
            # user_id on a locked/transferred lead even though they'd otherwise fail
            # _mz_can_edit_by_team/_mz_can_edit_owned below. Scoped to a write that
            # touches ONLY user_id (besides system fields); any other content field in
            # the same write still goes through the normal gate, so this doesn't become
            # a backdoor for editing locked/transferred lead content generally.
            dmt_reassign_only = content_touched == {"user_id"} and self._mz_user_is_dmt(u)

            if content_touched and not dmt_reassign_only:
                for lead in self:
                    if lead.x_is_locked:
                        if not lead.can_user_release_lock(u):
                            raise AccessError(_(
                                "Lead '%s' is RED-locked and read-only. Use 'Release RED Lock' "
                                "before it can be edited again."
                            ) % lead.name)
                    elif not lead._mz_can_edit_owned(u):
                        raise AccessError(_(
                            "Lead '%s' has been transferred to another team and is "
                            "read-only for you now."
                        ) % lead.name)

            # BU Manager Content Lock: a Manager can view/reassign every lead in their scope
            # (granted by the ir.rule Team Lead tier, which Manager inherits via implied_ids),
            # but content edits on a lead they don't own go through the Team Lead instead.
            if any(u.has_group(g) for g in MZR_MANAGER_GROUPS):
                for lead in self:
                    if lead.user_id and lead.user_id != u:
                        touched_content = content_touched
                        if touched_content - REASSIGN_FIELDS:
                            raise AccessError(_("Managers view and reassign; content edits go through the Team Lead."))

        # M3 stage-mandatory-field gate: enforced regardless of role (CTO/Admin included -
        # this is a data-completeness rule, not an authority one), skipped only for raw
        # su/system writes (migrations, demo-data seeding) so those aren't forced to
        # pre-fill every mandatory field for stages they're placing records into directly.
        if 'stage_id' in vals and not self.env.su:
            new_stage = self.env['crm.stage'].browse(vals['stage_id'])
            for lead in self:
                lead._mz_stage_gate_check(new_stage, vals)

        return super(CrmLead, self).write(vals)

    def unlink(self):
        if not self.env.su:
            raise AccessError(_("Deletion of leads is disabled for all roles. Please use the Archive Lead Wizard to archive leads."))
        return super(CrmLead, self).unlink()

    # (Agent-tier, ATL-tier, Team Lead-tier, BU Manager-tier) group chains from
    # mazenet_access_rights, independent of crm.team. teams.xml consolidates Hunter/
    # Account Manager/Corporate Training/LMS/TNH into one team_corporate record, and
    # Tally's Development/Sales branches into one team_tally record - but the
    # access-rights GROUP hierarchy stays fully separate per sub-team regardless (e.g.
    # group_mzr_hunter_tl is not the same group as group_mzr_lms_tl). That means a
    # single crm.team can no longer be mapped to one fixed tier-group tuple, so both
    # RED-lock release authority (_mz_team_tier_groups) and the "Team"/"Internal" assign-type
    # gate (_compute_x_assignable_user_ids) resolve tier from a user's actual
    # group membership instead of from a crm.team: each user can only belong to one of
    # these chains, so checking which one they hold gives an unambiguous answer
    # regardless of how teams.xml groups crm.team records.
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

    def _mz_team_tier_groups(self):
        """(tl_group, manager_group) xmlids (unqualified, mazenet_access_rights module)
        for this lead's OWNER, resolved from their actual group membership - or None if
        the owner isn't in any recognized chain."""
        self.ensure_one()
        owner = self.user_id
        if not owner:
            return None
        for agent_group, atl_group, tl_group, manager_group in self.MZR_TIER_GROUP_CHAINS:
            if (owner.has_group(f'mazenet_access_rights.{agent_group}')
                    or owner.has_group(f'mazenet_access_rights.{atl_group}')
                    or owner.has_group(f'mazenet_access_rights.{tl_group}')
                    or owner.has_group(f'mazenet_access_rights.{manager_group}')):
                return (tl_group, manager_group)
        return None

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

    def _mz_release_head_group(self):
        """The mazenet_access_rights group whose members (directly, or via implied_ids from
        a higher tier) are authorized to release this lead's RED lock - one tier above
        whichever tier the lead's own owner holds. Returns None if the owner is themselves
        at Manager-tier (their own lead: self-release, or CTO/Admin - handled by the callers,
        not by a team group), or if the team/owner aren't recognized."""
        self.ensure_one()
        owner = self.user_id
        chain = self._mz_team_tier_groups()
        if not owner or not chain:
            return False
        tl_group, manager_group = chain
        if owner.has_group(f'mazenet_access_rights.{manager_group}'):
            return None
        if owner.has_group(f'mazenet_access_rights.{tl_group}'):
            return manager_group
        return tl_group

    def can_user_release_lock(self, target_user=None):
        """Per the release policy: CTO/Admin always can; otherwise whoever holds the group
        one tier above the lead owner's own tier (their "head", which - through implied_ids -
        also covers anyone further up, their "superior"); a Manager's own lead is releasable
        by that Manager themselves (self-release) besides CTO/Admin."""
        self.ensure_one()
        u = target_user or self.env.user
        owner = self.user_id
        if not owner:
            return True
        if self.env.su or u.has_group('mazenet_access_rights.group_mzr_cto_admin'):
            return True
        head_group = self._mz_release_head_group()
        if head_group is None:
            return owner == u
        if not head_group:
            return False
        return u.has_group(f'mazenet_access_rights.{head_group}')

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
        that tier too."""
        self.ensure_one()
        if not self.team_id or user not in self.team_id.member_ids:
            return False
        tier, _chain = self._mz_user_tier_chain(user)
        return tier in ('atl', 'tl', 'manager')

    def _mz_can_edit_owned(self, user):
        """Whether `user` may edit this UNLOCKED lead's content - owned or not, name
        aside this is the general-purpose check for the non-locked case. Sales Team
        (team_id) is the single source of truth, no owner exemption - `user` must
        currently be a member of team_id.member_ids, full stop. Within that, two
        cases: the lead's OWNER (if any - an unowned lead never matches this) may
        edit it at ANY tier (an Agent editing their own lead is normal day-to-day CRM
        use, not something this rule should block) as long as they're still on the
        team it's filed under; anyone else - including on an unowned lead, where this
        is the ONLY path in - additionally needs ATL/TL/Manager tier
        (_mz_can_edit_by_team). Either way, the moment team_id moves elsewhere,
        whoever isn't a member of the NEW team loses access - owner included -
        matching how the transfer itself only ever considers Sales Team, nothing
        else. EXCEPT for DMT: a DMT team member is exempt from this whole
        Sales-Team-membership gate (same waiver as assignment - see
        _compute_x_assignable_user_ids) - they can edit a lead regardless of
        which team it's currently filed under, so they never hit the "transferred
        to another team" read-only message. RED-lock read-only (_mz_can_edit_by_team,
        used directly in write() while locked) is untouched by this - DMT still
        respects RED lock like everyone else."""
        self.ensure_one()
        dmt_team = self.env.ref('mazenet_crm.team_dmt', raise_if_not_found=False)
        if dmt_team and self._mz_user_own_team(user) == dmt_team:
            return True
        if not self.team_id or user not in self.team_id.member_ids:
            return False
        if user == self.user_id:
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
        is more than MZ_RED_LOCK_GRACE_MINUTES in the past. A 10:00 AM activity locks at
        10:15, not the instant 10:00 passes - gives the owner a short window to still make
        it before it counts against them."""
        now = fields.Datetime.now()
        current_company = self.env.company
        grace_time = current_company.grace_time
        cutoff = now - timedelta(minutes=grace_time)

        leads = self.sudo().search([
            ('x_next_activity_datetime', '!=', False),
            ('x_next_activity_datetime', '<', cutoff),
            ('x_is_locked', '=', False),
            ('active', '=', True),
            ('user_id', '!=', False),
        ])
        for lead in leads:
            lead.write({
                'x_is_locked': True,
                'x_lock_date': now,
            })
            lead._notify_red_lock_triggered()
    


    def _get_parent_hierarchy(self, group):
            """Recursively fetch all parent/ancestor groups."""
            parents = self.env['res.groups'].search([('implied_ids', 'in', group.id)])
            for parent in parents:
                parents |= self._get_parent_hierarchy(parent)
            return parents

    def check_red_lock_recods(self):
        red_lock_rec_vals = self.search([('x_is_locked', '=', True)])
        company = self.env.company
        grace_time = company.grace_time or 0
        todo_activity_type = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        activity_type_id = todo_activity_type.id if todo_activity_type else False
        for lead in red_lock_rec_vals:
            if lead.x_lock_date and (fields.Datetime.now() - lead.x_lock_date).total_seconds() / 60 > grace_time:
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
                                f"<p>Grace period of {grace_time} minutes has been exceeded.</p>"
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
