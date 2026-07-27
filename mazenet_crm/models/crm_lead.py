# -*- coding: utf-8 -*-
from datetime import datetime, timedelta

from odoo import models, fields, api, _
from odoo.exceptions import AccessError

REASSIGN_FIELDS = {"user_id", "team_id", "stage_id"}
SYSTEM_FIELDS = {
    "message_follower_ids", "activity_ids", "message_ids", "message_main_attachment_id",
    "website_message_ids", "message_has_error", "message_has_error_counter", "message_needaction",
    "message_needaction_counter", "message_is_follower", "message_partner_ids", "activity_state",
    "activity_user_id", "activity_type_id", "activity_date_deadline", "activity_summary",
    "activity_exception_type", "activity_exception_decoration", "active",
    "x_is_locked", "x_lock_date", "x_lock_escalated"
}

class CrmLead(models.Model):
    _inherit = "crm.lead"
    _order = "x_next_activity_datetime asc, priority desc, id desc"

    x_next_activity_datetime = fields.Datetime(
        string="Next Activity Time",
        compute="_compute_x_next_activity_datetime",
        store=True,
        index=True,
        help="Earliest open activity's actual moment - a meeting's real start time (e.g. "
             "10:00 AM sorts above an 11:00 AM call the same day), or midnight of the due "
             "date for activities with no specific time. Drives the default Kanban/List "
             "ordering (soonest activity on top) instead of crm.lead's stock priority/id "
             "order; leads with no open activity naturally sort to the bottom (NULL last)."
    )

    @api.depends('activity_ids.date_deadline', 'activity_ids.calendar_event_id.start', 'activity_ids.active')
    def _compute_x_next_activity_datetime(self):
        for lead in self:
            candidates = []
            for activity in lead.activity_ids.filtered('active'):
                if activity.calendar_event_id and activity.calendar_event_id.start:
                    candidates.append(activity.calendar_event_id.start)
                elif activity.date_deadline:
                    candidates.append(datetime.combine(activity.date_deadline, datetime.min.time()))
            lead.x_next_activity_datetime = min(candidates) if candidates else False

    x_originating_team_id = fields.Many2one(
        "crm.team",
        string="Originating Team (DMT)",
        help="Tracks originating team for leads created by or transferred from DMT."
    )

    x_is_locked = fields.Boolean(
        string="RED Lock Active",
        default=False,
        help="Indicates if lead is currently locked due to RED timer expiration."
    )

    x_lock_date = fields.Datetime(
        string="RED Lock Date",
        help="Timestamp when RED lock was triggered."
    )

    x_lock_escalated = fields.Boolean(
        string="RED Lock Escalated",
        default=False,
        help="True once the 24h auto-escalation notification has been sent for the current lock."
    )

    @api.model_create_multi
    def create(self, vals_list):
        u = self.env.user
        if not self.env.su and u.has_group("mazenet_crm.group_mz_md") and not u.has_group("mazenet_crm.group_mz_admin"):
            raise AccessError(_("MD role is read-only across all CRM models and cannot create leads."))
        return super(CrmLead, self).create(vals_list)

    def write(self, vals):
        u = self.env.user

        # 0. Direct Lead Archiving Restriction (CTO / Admin Only via Wizard)
        if "active" in vals and not vals["active"]:
            if not self.env.su and not u.has_group("mazenet_crm.group_mz_admin") and not self.env.context.get("mz_archive_wizard"):
                raise AccessError(_("Leads can only be archived by CTO / Admin via the Archive Lead Wizard."))

        # Track leads currently sitting in a DMT team that this write is about to move to a
        # different team, so we can stamp x_originating_team_id once the move succeeds.
        # Section 4's DMT read-only tracking normally gets populated by the transfer wizard
        # (M2/M3, not built yet) - until then, a plain team reassignment away from DMT
        # should still mark the lead as DMT-originated automatically, since that's what
        # flips the read-only-after-transfer restriction below (and the cross-BU tracking
        # rule in record_rules.xml) on.
        dmt_origin_map = {}
        if 'team_id' in vals and vals['team_id']:
            for lead in self:
                if (lead.team_id and lead.team_id.x_bu_category == 'dmt'
                        and lead.team_id.id != vals['team_id']
                        and not lead.x_originating_team_id):
                    dmt_origin_map[lead.id] = lead.team_id.id

        # Bypass checks for sudo / superuser or CTO Admin group
        if not self.env.su and not u.has_group("mazenet_crm.group_mz_admin"):

            # 1. MD Read-Only Restriction
            if u.has_group("mazenet_crm.group_mz_md"):
                raise AccessError(_("MD role is read-only across all CRM models and cannot edit leads."))

            touched_keys = set(vals.keys())

            # 1.5 RED Lock Enforcement (Section 5): a locked lead is read-only for EVERYONE
            # (including its own owner, TL, Manager) until the authorized releaser explicitly
            # releases it via action_release_lock(). Only bookkeeping/system fields (chatter,
            # activities, and the lock fields themselves - so the release action can clear
            # them) are exempt.
            content_touched = touched_keys - SYSTEM_FIELDS
            if content_touched:
                for lead in self:
                    if lead.x_is_locked:
                        releaser = lead._get_lock_release_target()
                        raise AccessError(_(
                            "Lead '%s' is RED-locked and read-only. %s (or CTO) must release the "
                            "lock before it can be edited again."
                        ) % (lead.name, releaser.name if releaser else _("its supervisor")))

            # 1.6 DMT Read-Only-After-Transfer: once a lead has been transferred out of DMT
            # to another team, DMT members are read-only on it (same restriction MD has
            # everywhere, scoped here to just that one lead) - they keep tracking visibility
            # (rule_crm_lead_dmt_tracking) but no further edits, including reassignment.
            # Checked before the Manager/Agent branches below so a DMT Manager can't use the
            # reassignment-fields carve-out to touch a lead that's already left DMT.
            if content_touched and 'dmt' in u.crm_team_ids.mapped('x_bu_category'):
                for lead in self:
                    if (lead.x_originating_team_id
                            and lead.x_originating_team_id.x_bu_category == 'dmt'
                            and lead.team_id != lead.x_originating_team_id):
                        raise AccessError(_(
                            "This lead was transferred out of DMT to '%s'. DMT members have "
                            "read-only tracking visibility only - further edits go through the "
                            "receiving team."
                        ) % lead.team_id.name)

            # 2. BU Manager Content Lock Restriction
            if u.has_group("mazenet_crm.group_mz_manager"):
                for lead in self:
                    if lead.user_id and lead.user_id != u:
                        touched_content = touched_keys - SYSTEM_FIELDS
                        if touched_content - REASSIGN_FIELDS:
                            raise AccessError(_("Managers view and reassign; content edits go through the Team Lead."))

            # 3. Agent Edit Restriction
            elif u.has_group("mazenet_crm.group_mz_agent") and not u.has_group("mazenet_crm.group_mz_tl"):
                for lead in self:
                    if lead.user_id and lead.user_id != u:
                        touched_content = touched_keys - SYSTEM_FIELDS
                        if touched_content:
                            raise AccessError(_("Agents can only edit their own assigned leads."))

        res = super(CrmLead, self).write(vals)

        for lead_id, origin_team_id in dmt_origin_map.items():
            self.browse(lead_id).sudo().write({'x_originating_team_id': origin_team_id})

        return res

    def unlink(self):
        if not self.env.su:
            raise AccessError(_("Deletion of leads is disabled for all roles, including CTO/Admin. Please use the CTO Archive Lead Wizard to archive leads."))
        return super(CrmLead, self).unlink()

    def can_user_release_lock(self, target_user=None, lock_duration_hours=0):
        """
        Validates if target_user can release RED lock on this lead per Section 7.4 Matrix:
        - Agent under ATL -> ATL only
        - Agent under TL -> TL only
        - Manager-direct agent -> Manager only
        - ATL's own lead -> TL only
        - TL's own lead -> Manager only
        - Manager's own lead -> Manager self-release (popup/notification to MD+CTO)
        - Fallback 1: CTO override anytime
        - Fallback 2: 24h escalation -> level 2 supervisor
        """
        self.ensure_one()
        u = target_user or self.env.user
        owner = self.user_id

        if not owner:
            return (True, "Unassigned Lead")

        # Fallback 1: CTO / Admin override anytime
        if u.has_group("mazenet_crm.group_mz_admin") or u._is_superuser():
            return (True, "CTO Override")

        # Manager self-release
        if owner == u and u.has_group("mazenet_crm.group_mz_manager"):
            return (True, "Manager Self-Release")

        # Direct Supervisor check
        direct_sup = self.env["mz.supervision"].get_approver(owner)
        if direct_sup and u == direct_sup:
            return (True, "Direct Supervisor Release")

        # Fallback 2: 24h Auto-escalation check
        if lock_duration_hours >= 24:
            escalated_sup = self.env["mz.supervision"].get_escalated_approver(owner)
            if escalated_sup and u == escalated_sup:
                return (True, "24h Escalation Release")

        return (False, "Not Authorized")

    def _lock_duration_hours(self):
        self.ensure_one()
        if not self.x_lock_date:
            return 0.0
        delta = fields.Datetime.now() - self.x_lock_date
        return delta.total_seconds() / 3600.0

    def _get_lock_release_target(self):
        """The correct releaser for this lead per the Section 5 matrix, independent of who
        is asking (used for error messages and for routing the release notification)."""
        self.ensure_one()
        owner = self.user_id
        if not owner:
            return self.env['res.users']
        if owner.has_group("mazenet_crm.group_mz_manager"):
            return owner  # Manager's own lead: manager self-releases
        return self.env["mz.supervision"].get_approver(owner) or self.env['res.users']

    def _get_lock_notify_users(self):
        """Users notified when the lock triggers (Section 5 'Notified' column). Same as the
        releaser, except a Manager's own lead notifies MD + CTO instead (never asked to act)."""
        self.ensure_one()
        owner = self.user_id
        if not owner:
            return self.env['res.users']
        if owner.has_group("mazenet_crm.group_mz_manager"):
            md_group = self.env.ref('mazenet_crm.group_mz_md', raise_if_not_found=False)
            admin_group = self.env.ref('mazenet_crm.group_mz_admin', raise_if_not_found=False)
            users = self.env['res.users']
            if md_group:
                users |= md_group.user_ids
            if admin_group:
                users |= admin_group.user_ids
            return users
        return self._get_lock_release_target()

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
        a standing activity for the releaser when a lock triggers."""
        self.ensure_one()
        releaser = self._get_lock_release_target()
        notify_users = self._get_lock_notify_users()
        owner_name = self.user_id.name if self.user_id else _("Unassigned")

        self.message_post(
            body=_("RED LOCK triggered: lead is overdue and now read-only (owner: %s).") % owner_name,
            subtype_xmlid="mail.mt_note",
        )

        self._push_notification(
            releaser | notify_users,
            subject=_("RED Lock: Action Required"),
            body=_("Lead '%s' (owner: %s) is RED-locked and needs your release approval.") % (self.name, owner_name),
        )

        if releaser:
            self.activity_schedule(
                'mail.mail_activity_data_todo',
                summary=_("Release RED Lock: %s") % self.name,
                note=_("This lead is overdue and RED-locked. Review it and use 'Release RED Lock' to make it editable again."),
                user_id=releaser.id,
            )

    @api.model
    def _cron_trigger_red_locks(self):
        """M2: auto-trigger the RED lock on leads that are overdue - either by date (the
        activity's due date has already passed) or, same-day, by time (a meeting activity
        whose actual start time has already passed - e.g. a 12:15pm meeting is overdue at
        12:16pm even though 'today' hasn't changed yet).

        mail.activity.date_deadline is a Date field with no time component (Section 7.7:
        'do not add parallel date fields' - so no separate timer field is added here
        either). The time-of-day only exists on the linked calendar.event (via
        calendar_event_id, added by the 'calendar' module crm already depends on) for
        activities scheduled as meetings, so that's what the same-day check reads."""
        now = fields.Datetime.now()
        today = fields.Date.context_today(self)

        overdue_activities = self.env['mail.activity'].sudo().search([
            ('res_model', '=', 'crm.lead'),
            '|',
                ('date_deadline', '<', today),
                '&', '&',
                    ('date_deadline', '=', today),
                    ('calendar_event_id', '!=', False),
                    ('calendar_event_id.start', '<', now),
        ])
        lead_ids = overdue_activities.mapped('res_id')

        leads = self.sudo().browse(lead_ids).exists().filtered(
            lambda l: not l.x_is_locked and l.active and l.user_id
        )
        for lead in leads:
            lead.write({
                'x_is_locked': True,
                'x_lock_date': now,
                'x_lock_escalated': False,
            })
            lead._notify_red_lock_triggered()

    @api.model
    def _cron_escalate_red_locks(self):
        """Section 5 Fallback 2: a lock unreleased for 24h notifies one level up, who can then
        ALSO release. Normal routing stays strict - this only adds an extra notified/authorized
        party, it doesn't change who the primary releaser is."""
        cutoff = fields.Datetime.now() - timedelta(hours=24)
        leads = self.sudo().search([
            ('x_is_locked', '=', True),
            ('x_lock_escalated', '=', False),
            ('x_lock_date', '!=', False),
            ('x_lock_date', '<=', cutoff),
        ])
        for lead in leads:
            escalated_sup = self.env["mz.supervision"].get_escalated_approver(lead.user_id) if lead.user_id else False
            lead.x_lock_escalated = True
            lead.message_post(
                body=_("RED lock unreleased for 24h+. Escalated to %s.") % (
                    escalated_sup.name if escalated_sup else _("CTO/Admin")),
                subtype_xmlid="mail.mt_note",
            )
            if escalated_sup:
                lead._push_notification(
                    escalated_sup,
                    subject=_("RED Lock Escalation"),
                    body=_("Lead '%s' has been locked 24h+ without release. You may now release it too.") % lead.name,
                )
                lead.activity_schedule(
                    'mail.mail_activity_data_todo',
                    summary=_("[Escalated] Release RED Lock: %s") % lead.name,
                    note=_("This lead has been RED-locked for 24h+. As the escalation contact, you may release it."),
                    user_id=escalated_sup.id,
                )

    def action_release_lock(self):
        """Action method to release RED lock on lead with chatter & MD/CTO notification"""
        for lead in self:
            u = self.env.user
            lock_duration = lead._lock_duration_hours()

            allowed, reason = lead.can_user_release_lock(u, lock_duration_hours=lock_duration)
            if not allowed:
                raise AccessError(_("You are not authorized to release the RED lock on lead '%s'. Direct supervisor or CTO authorization required.") % lead.name)

            # Manager self-release notification to MD & CTO
            if reason == "Manager Self-Release":
                msg = _("Manager %s self-released RED lock on lead '%s'. Notification sent to MD & CTO.") % (u.name, lead.name)
                lead.message_post(body=msg, subtype_xmlid="mail.mt_note")
                lead._push_notification(
                    lead._get_lock_notify_users(),
                    subject=_("Manager Self-Release"),
                    body=_("Manager %s self-released the RED lock on lead '%s'.") % (u.name, lead.name),
                )

            lead.write({
                'x_is_locked': False,
                'x_lock_date': False,
                'x_lock_escalated': False,
            })
            lead.message_post(body=_("RED lock released by %s (%s). Lead is editable again.") % (u.name, reason))

            if lead.user_id:
                lead._push_notification(
                    lead.user_id,
                    subject=_("RED Lock Released"),
                    body=_("Lead '%s' has been released by %s and is editable again.") % (lead.name, u.name),
                )

        return True

    # Team xmlid -> (company name pool, lead-name template). Corp category is split per
    # actual team (Hunter/AM/LMS/TNH), not lumped by x_bu_category, since they represent
    # different lines of business despite sharing the "corp" category.
    _MZ_TEAM_LEAD_POOLS = {
        'mazenet_crm.team_dmt': (
            ["Rajesh Traders", "Sunrise Textiles", "Om Sai Enterprises", "Kaveri Foods Pvt Ltd",
             "Shree Balaji Hardware", "New Bharat Stationers", "Ganpati Agro Foods", "Vinayak Plastics"],
            "Inquiry - %s", 15000,
        ),
        'mazenet_crm.team_tally': (
            ["Sharma & Sons Traders", "Golden Textiles Mills", "Anand Auto Spares", "Krishna Rice Mill",
             "Vishal Electricals", "Om Enterprises", "Patel Hardware Store", "Laxmi Garments"],
            "Tally License - %s", 25000,
        ),
        'mazenet_crm.team_corp_hunter': (
            ["Meridian Logistics Pvt Ltd", "Zenith Manufacturing Corp", "Apex Infrastructure Ltd", "Orion Retail Chain",
             "Falcon Energy Solutions", "Skyline Constructions", "Prime Steel Industries", "Coastal Shipping Corp"],
            "New Business - %s", 150000,
        ),
        'mazenet_crm.team_corp_am': (
            ["Meridian Logistics Pvt Ltd", "Zenith Manufacturing Corp", "Apex Infrastructure Ltd", "Orion Retail Chain",
             "Falcon Energy Solutions", "Skyline Constructions", "Prime Steel Industries", "Coastal Shipping Corp"],
            "Account Renewal - %s", 100000,
        ),
        'mazenet_crm.team_corp_lms': (
            ["Bright Future Public School", "Global Institute of Technology", "Sunrise Degree College",
             "National Skill Academy", "Everest Public School", "Coastal Management Institute"],
            "LMS Deal - %s", 60000,
        ),
        'mazenet_crm.team_corp_tnh': (
            ["Blue Orchid Resorts", "Grand Palace Hotels", "Coastal Getaway Resorts", "Heritage Inn Group",
             "Emerald Beach Resort", "Silver Sands Hotel"],
            "TNH Deal - %s", 80000,
        ),
        'mazenet_crm.team_tech': (
            ["NextGen Solutions", "Skyline Systems", "Vertex Apps", "Quantum Labs",
             "Bluewave Technologies", "Ironclad Networks"],
            "Tech Project - %s", 90000,
        ),
        'mazenet_crm.team_swdev': (
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
        """Demo-data generator: gives every @test.mazenet user (except CTO/MD, who don't own
        leads per their role) `leads_per_user` business-appropriate leads, spread across
        their own team's real stages. Idempotent - re-running this (it's called from
        demo_data.xml on every install/update) tops a user up to the target count rather
        than creating duplicates on top of what they already have."""
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
