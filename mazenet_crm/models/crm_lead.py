# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import models, fields, api, _
from odoo.exceptions import AccessError

# Grace period between an activity's real due moment (x_next_activity_datetime) and the
# RED lock actually triggering - e.g. a 10:00 AM activity locks at 10:15, not the instant
# 10:00 passes.
MZ_RED_LOCK_GRACE_MINUTES = 15

REASSIGN_FIELDS = {"user_id", "team_id", "stage_id"}
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


    @api.onchange('team_id')
    def assign_salesperson(self):
        self.user_id = self.sudo().team_id.create_lead_id or False

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

    @api.model_create_multi
    def create(self, vals_list):
        u = self.env.user
        if not self.env.su and u.has_group("mazenet_access_rights.group_mzr_md"):
            raise AccessError(_("MD role is read-only across all CRM models and cannot create leads."))
        return super(CrmLead, self).create(vals_list)

    def write(self, vals):
        u = self.env.user
        is_cto_admin = u.has_group("mazenet_access_rights.group_mzr_cto_admin")

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

            # RED Lock Enforcement: a locked lead is read-only for everyone until released via
            # action_release_lock(). Only bookkeeping/system fields (chatter, activities, and
            # the lock fields themselves - so the release action can clear them) are exempt.
            if content_touched:
                for lead in self:
                    if lead.x_is_locked:
                        raise AccessError(_(
                            "Lead '%s' is RED-locked and read-only. Use 'Release RED Lock' "
                            "before it can be edited again."
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

    # (Agent-tier group, Team Lead-tier group, BU Manager-tier group) chains from
    # mazenet_access_rights, independent of crm.team. teams.xml consolidates Hunter/
    # Account Manager/Corporate Training/LMS/TNH into one team_corporate record, and
    # Tally's Development/Sales branches into one team_tally record - but the
    # access-rights GROUP hierarchy stays fully separate per sub-team regardless (e.g.
    # group_mzr_hunter_tl is not the same group as group_mzr_lms_tl). That means a
    # single crm.team can no longer be mapped to one fixed (tl_group, manager_group)
    # pair, so RED-lock release authority is resolved from the lead OWNER's actual
    # group membership instead of from the lead's team_id: each owner can only be a
    # member of exactly one of these chains, so checking which one they hold still
    # gives an unambiguous answer, and it stays correct however teams.xml is organized.
    MZR_TIER_GROUP_CHAINS = [
        ('group_mzr_dmt_agent', 'group_mzr_dmt_tl', 'group_mzr_dmt_manager'),
        ('group_mzr_technology_agent', 'group_mzr_technology_tl', 'group_mzr_technology_manager'),
        ('group_mzr_software_agent', 'group_mzr_software_tl', 'group_mzr_software_manager'),
        ('group_mzr_mis_agent', 'group_mzr_mis_tl', 'group_mzr_mis_manager'),
        ('group_mzr_hunter_agent', 'group_mzr_hunter_tl', 'group_mzr_corporate_manager'),
        ('group_mzr_account_manager_agent', 'group_mzr_account_manager_tl', 'group_mzr_corporate_manager'),
        ('group_mzr_corporate_training_agent', 'group_mzr_corporate_training_tl', 'group_mzr_corporate_manager'),
        ('group_mzr_lms_agent', 'group_mzr_lms_tl', 'group_mzr_corporate_manager'),
        ('group_mzr_tnh_agent', 'group_mzr_tnh_tl', 'group_mzr_corporate_manager'),
        ('group_mzr_tally_atl_agents_dev', 'group_mzr_tally_tl_development', 'group_tally_manager'),
        ('group_mzr_tally_atl_agents_sales', 'group_mzr_tally_tl_sales', 'group_tally_manager'),
    ]

    def _mz_team_tier_groups(self):
        """(tl_group, manager_group) xmlids (unqualified, mazenet_access_rights module)
        for this lead's OWNER, resolved from their actual group membership - or None if
        the owner isn't in any recognized chain."""
        self.ensure_one()
        owner = self.user_id
        if not owner:
            return None
        for agent_group, tl_group, manager_group in self.MZR_TIER_GROUP_CHAINS:
            if (owner.has_group(f'mazenet_access_rights.{agent_group}')
                    or owner.has_group(f'mazenet_access_rights.{tl_group}')
                    or owner.has_group(f'mazenet_access_rights.{manager_group}')):
                return (tl_group, manager_group)
        return None

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
        cutoff = now - timedelta(minutes=MZ_RED_LOCK_GRACE_MINUTES)

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

    # Team xmlid -> (company name pool, lead-name template, base revenue). teams.xml
    # consolidated Hunter/Account Manager/Corporate Training/LMS/TNH into one
    # team_corporate record, and Tally's Development/Sales branches into one
    # team_tally record, so their formerly-separate pools are merged here too
    # (company lists combined for variety; template/revenue picked as one
    # representative value rather than kept per sub-team, since crm.team no
    # longer distinguishes them).
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
