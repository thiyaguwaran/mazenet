# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class CalendarEvent(models.Model):
    _inherit = "calendar.event"

    x_meeting_status = fields.Selection(
        [
            ("not_started", "Not Started"),
            ("in_progress", "In Progress"),
            ("done", "Done"),
        ],
        string="Meeting Status",
        default="not_started",
        help="Manually tracked by the attendee: lets the team see at a glance whether a "
             "scheduled meeting hasn't started yet, is happening right now, or has wrapped "
             "up - Odoo has no built-in field for this, only the start/stop timestamps."
    )

    @api.model_create_multi
    def create(self, vals_list):
        events = super().create(vals_list)
        events._mz_check_not_scheduled_in_past()
        events._attach_mz_reminders()
        return events

    def write(self, vals):
        res = super().write(vals)
        if 'start' in vals:
            self._mz_check_not_scheduled_in_past()
        if 'res_model' in vals or 'res_id' in vals:
            self._attach_mz_reminders()
        if vals.get('x_meeting_status') == 'done':
            self._complete_linked_crm_activities()
        return res

    def _mz_check_not_scheduled_in_past(self):
        """Reject scheduling/moving a CRM-lead meeting to a start time that's already
        passed - mirrors the same rule enforced for Call/To-Do activities in
        mail_activity.py's _mz_check_not_scheduled_in_past. Scoped to res_model ==
        'crm.lead' so it doesn't affect general calendar usage outside the CRM."""
        now = fields.Datetime.now()
        for event in self:
            if event.res_model == 'crm.lead' and event.start and event.start < now:
                raise UserError(_(
                    "Can't schedule '%s' in the past. Pick a date/time that hasn't passed yet."
                ) % event.name)

    def _attach_mz_reminders(self):
        """Auto-attach the 30/10-minute popup reminders to meetings scheduled from a CRM
        lead, so attendees get warned before a meeting that (per the RED lock logic) will
        count against them the moment it starts."""
        crm_events = self.filtered(lambda e: e.res_model == 'crm.lead')
        if not crm_events:
            return
        alarm_30 = self.env.ref('mazenet_crm.calendar_alarm_30min', raise_if_not_found=False)
        alarm_10 = self.env.ref('mazenet_crm.calendar_alarm_10min', raise_if_not_found=False)
        alarms = (alarm_30 or self.env['calendar.alarm']) | (alarm_10 or self.env['calendar.alarm'])
        if alarms:
            crm_events.write({'alarm_ids': [(4, alarm.id) for alarm in alarms]})

    def _complete_linked_crm_activities(self):
        """Marking the meeting Done also completes its linked mail.activity (single source
        of truth for 'is this activity still open' - Section 7.7: no parallel state). This
        does NOT touch any RED lock already triggered on the lead: if the meeting was missed
        and the lock already fired, closing it out here doesn't self-release the lock - that
        still goes through the normal approver release flow (Section 5)."""
        for event in self:
            open_activities = event.activity_ids.filtered(
                lambda a: a.res_model == 'crm.lead' and a.active
            )
            if open_activities:
                open_activities._action_done()
