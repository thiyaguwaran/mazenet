# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import models, fields, api, _
from odoo.exceptions import AccessError

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

    team_id = fields.Many2one(default=_mz_default_team_id)

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
             "- Internal: hand-picked from any member of the selected team.\n"
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
             "'Internal': all members of the selected team. Used as user_id's domain "
             "in the view; not stored, purely a UI helper. An onchange-returned domain "
             "isn't reliably honored by the web client for Many2one search, so the "
             "domain lives in the view via this computed field instead."
    )
    x_is_dmt_user = fields.Boolean(
        compute='_compute_x_assignable_user_ids',
        string="Is DMT User",
        help="Whether the CURRENT user (viewing/editing this lead right now) belongs "
             "to the DMT team - used in the view to exempt user_id from the general "
             "x_content_readonly_for_me gate for DMT, on top of the unrestricted "
             "assignment pool above. Not stored - reflects whoever has the form open."
    )

    @api.depends('team_id', 'x_assign_type')
    @api.depends_context('uid')
    def _compute_x_assignable_user_ids(self):
        """DMT is a special case: a DMT team member may assign to ANY of the team's
        users under both 'Team' and 'Internal' - no create_lead_id narrowing, and no
        ATL/TL/Manager tier gate either (DMT membership itself is enough - the
        Agent-restricted-to-Self rule is entirely waived for DMT). Everyone else
        keeps the normal tier-gated behavior: 'Team' restricted to create_lead_id
        members, 'Internal' to the full team roster. See _mz_check_assign_type_allowed
        for the matching server-side backstop - it must waive the tier gate for DMT
        the same way, or a DMT Agent could pick 'Team'/'Internal' here and then get
        rejected on save."""
        user = self.env.user
        dmt_team = self.env.ref('mazenet_crm.team_dmt', raise_if_not_found=False)
        user_is_dmt = bool(dmt_team) and self._mz_user_own_team(user) == dmt_team
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
                lead.x_assignable_user_ids = lead.team_id.member_ids

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

    def _mz_check_assign_type_allowed(self, vals):
        """Server-side backstop for x_assign_type in ('team', 'internal'): the view
        only offers those to ATL/TL/Manager tier (x_can_assign_beyond_self), and the
        onchange bounces an Agent back to 'self' and clears user_id if it falls outside
        x_assignable_user_ids - but all of that is UI-only, so a direct RPC/API write
        could still set either. Raises the same way the UI would have refused, instead
        of silently accepting it. Mirrors _compute_x_assignable_user_ids' DMT waiver:
        a DMT team member skips the tier gate entirely and gets the full team roster
        as their pool for both 'team' and 'internal' - keep the two in sync, or a DMT
        Agent could pick 'Team'/'Internal' in the UI and then get rejected on save."""
        assign_type = vals.get('x_assign_type')
        if assign_type not in ('team', 'internal') or self.env.su:
            return
        user = self.env.user
        dmt_team = self.env.ref('mazenet_crm.team_dmt', raise_if_not_found=False)
        user_is_dmt = bool(dmt_team) and self._mz_user_own_team(user) == dmt_team
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
            else:
                pool_ids = (team.create_lead_id if assign_type == 'team' else team.member_ids).ids
            if vals['user_id'] not in pool_ids:
                raise AccessError(_(
                    "The selected salesperson isn't in the allowed assignment pool for "
                    "this team under '%s' assignment. Pick from the assignable list."
                ) % assign_type)

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
            # action_release_lock() - EXCEPT for the CURRENT team_id's ATL/TL/Manager
            # (_mz_can_edit_by_team), who can keep working it (including reassigning it
            # to a different team via team_id) as long as it's still on their team. The
            # moment such a reassignment lands, they stop qualifying for the NEW
            # team_id and lose that access too, same as anyone else not on it. Only
            # bookkeeping/system fields (chatter, activities, and the lock fields
            # themselves - so the release action can clear them) are exempt regardless.
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
            if content_touched:
                for lead in self:
                    if lead.x_is_locked:
                        if not lead._mz_can_edit_by_team(u):
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

        Used two ways in write() - while RED-locked, this is the ONLY non-CTO/Admin
        path to edit at all (the owner is deliberately excluded there too, being
        locked out is the point); once unlocked, _mz_can_edit_owned uses this for any
        NON-owner (a TL/ATL/Manager working a lead they don't personally own), OR'd
        with a separate, tier-agnostic membership check for the owner themselves."""
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
        else."""
        self.ensure_one()
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
