# -*- coding: utf-8 -*-
from datetime import timedelta

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

        return super(CrmLead, self).write(vals)

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

    def _notify_red_lock_triggered(self):
        """Chatter + bus popup + persistent activity to the releaser when a lock triggers."""
        self.ensure_one()
        releaser = self._get_lock_release_target()
        notify_users = self._get_lock_notify_users()
        owner_name = self.user_id.name if self.user_id else _("Unassigned")

        self.message_post(
            body=_("RED LOCK triggered: lead is overdue and now read-only (owner: %s).") % owner_name,
            subtype_xmlid="mail.mt_note",
        )

        for user in (releaser | notify_users):
            user._bus_send('simple_notification', {
                'title': _("RED Lock: Action Required"),
                'message': _("Lead '%s' (owner: %s) is RED-locked and needs your release approval.") % (self.name, owner_name),
                'type': 'danger',
                'sticky': True,
            })

        if releaser:
            self.activity_schedule(
                'mail.mail_activity_data_todo',
                summary=_("Release RED Lock: %s") % self.name,
                note=_("This lead is overdue and RED-locked. Review it and use 'Release RED Lock' to make it editable again."),
                user_id=releaser.id,
            )

    @api.model
    def _cron_trigger_red_locks(self):
        """M2: auto-trigger the RED lock on leads whose activity deadline is overdue. Reuses
        the existing activity_date_deadline field (Section 7.7: 'do not add parallel date
        fields') rather than a separate timer."""
        today = fields.Date.context_today(self)
        leads = self.sudo().search([
            ('x_is_locked', '=', False),
            ('activity_date_deadline', '!=', False),
            ('activity_date_deadline', '<', today),
            ('active', '=', True),
            ('user_id', '!=', False),
        ])
        for lead in leads:
            lead.write({
                'x_is_locked': True,
                'x_lock_date': fields.Datetime.now(),
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
                escalated_sup._bus_send('simple_notification', {
                    'title': _("RED Lock Escalation"),
                    'message': _("Lead '%s' has been locked 24h+ without release. You may now release it too.") % lead.name,
                    'type': 'danger',
                    'sticky': True,
                })
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
                for md_cto_user in lead._get_lock_notify_users():
                    md_cto_user._bus_send('simple_notification', {
                        'title': _("Manager Self-Release"),
                        'message': _("Manager %s self-released the RED lock on lead '%s'.") % (u.name, lead.name),
                        'type': 'warning',
                    })

            lead.write({
                'x_is_locked': False,
                'x_lock_date': False,
                'x_lock_escalated': False,
            })
            lead.message_post(body=_("RED lock released by %s (%s). Lead is editable again.") % (u.name, reason))

            if lead.user_id:
                lead.user_id._bus_send('simple_notification', {
                    'title': _("RED Lock Released"),
                    'message': _("Lead '%s' has been released by %s and is editable again.") % (lead.name, u.name),
                    'type': 'success',
                })

        return True
