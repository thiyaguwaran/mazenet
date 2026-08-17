# -*- coding: utf-8 -*-
from datetime import datetime, timedelta

import pytz

from odoo import models, fields, api, _
from odoo.exceptions import UserError

# Applied when an activity has neither a linked calendar event nor an explicit
# mz_activity_time - see crm_lead.py for why (keeps date-only activities from always
# sorting at midnight, ahead of every same-day timed activity).
MZ_DEFAULT_ACTIVITY_HOUR = 9.0


def mz_localize_to_utc(date_val, hour, tz_name):
    """A date + a float hour-of-day, localized in tz_name, as a naive UTC datetime -
    shared by mail.activity's date/time fields and the mail.activity.schedule wizard's
    mirror of them, so both resolve a Call/To-Do's time the same way."""
    naive_local = datetime.combine(date_val, datetime.min.time()) + timedelta(hours=hour)
    try:
        localized = pytz.timezone(tz_name).localize(naive_local)
    except pytz.exceptions.AmbiguousTimeError:
        localized = pytz.timezone(tz_name).localize(naive_local, is_dst=False)
    return localized.astimezone(pytz.UTC).replace(tzinfo=None)


class MailActivity(models.Model):
    _inherit = "mail.activity"

    mz_activity_time = fields.Float(
        string="Time",
        widget="float_time",
        help="Time of day for this activity. Odoo only stores a time for Meeting-category "
             "activities (via the linked calendar.event) - Calls and To-Dos have no time "
             "anywhere in the database otherwise, which is why two same-day activities of "
             "those types can't be told apart for sorting. Ignored for Meeting-category "
             "activities, whose time comes from calendar_event_id.start instead. Kept as a "
             "plain backing field - the form shows mz_activity_datetime instead, a single "
             "combined picker, so the user never edits this one directly."
    )

    mz_activity_datetime = fields.Datetime(
        string="Scheduled For",
        compute="_compute_mz_activity_datetime",
        inverse="_inverse_mz_activity_datetime",
        help="Single date+time picker for Call/To-Do/Email activities, shown instead of "
             "separate Due Date and Time fields - mirrors how a Meeting is scheduled as one "
             "step. Backed by the same date_deadline + mz_activity_time fields the RED-lock "
             "logic already reads, just split/joined transparently. Not used for Meeting "
             "activities, whose time comes from the linked calendar event instead."
    )

    @api.depends('date_deadline', 'mz_activity_time', 'activity_type_id')
    def _compute_mz_activity_datetime(self):
        for activity in self:
            if activity.activity_type_id.category == 'meeting' or not activity.date_deadline:
                activity.mz_activity_datetime = False
                continue
            hour = activity.mz_activity_time or MZ_DEFAULT_ACTIVITY_HOUR
            tz_name = (activity.user_id.tz if activity.user_id else self.env.user.tz) or 'UTC'
            activity.mz_activity_datetime = mz_localize_to_utc(activity.date_deadline, hour, tz_name)

    def _inverse_mz_activity_datetime(self):
        for activity in self:
            if not activity.mz_activity_datetime:
                continue
            tz_name = (activity.user_id.tz if activity.user_id else self.env.user.tz) or 'UTC'
            localized = pytz.UTC.localize(activity.mz_activity_datetime).astimezone(pytz.timezone(tz_name))
            activity.date_deadline = localized.date()
            activity.mz_activity_time = localized.hour + localized.minute / 60.0

    def _mz_resolve_activity_datetime(self):
        """Best-known actual moment for this activity, as a naive UTC datetime suitable for
        storing in a Datetime field: the linked calendar event's start if there is one,
        else date_deadline combined with mz_activity_time (localized in the responsible
        user's timezone), else date_deadline at MZ_DEFAULT_ACTIVITY_HOUR. Returns False if
        there's no date_deadline at all (activity_ids always require one, but be defensive).

        Meeting-category activities are a special case: Odoo creates the mail.activity row
        first and only attaches its calendar.event a moment later (once the user actually
        picks a date/time on the calendar form). Applying MZ_DEFAULT_ACTIVITY_HOUR in that
        gap would treat the activity as due at 9 AM on today's placeholder date_deadline -
        which is very often already in the past - and can get it RED-locked by the cron
        before the real meeting time is even known. So a meeting with no calendar event yet
        returns False (not yet resolved) instead of guessing.
        """
        self.ensure_one()
        if self.calendar_event_id and self.calendar_event_id.start:
            return self.calendar_event_id.start
        if self.activity_type_id.category == 'meeting':
            return False
        if not self.date_deadline:
            return False
        hour = self.mz_activity_time or MZ_DEFAULT_ACTIVITY_HOUR
        tz_name = (self.user_id.tz if self.user_id else self.env.user.tz) or 'UTC'
        return mz_localize_to_utc(self.date_deadline, hour, tz_name)

    def _mz_check_not_scheduled_in_past(self):
        """Reject Call/To-Do activities on a CRM lead whose resolved date+time has already
        passed. Called explicitly by the scheduling wizard once both date_deadline and
        mz_activity_time are set (not via @api.constrains - the wizard writes those two
        fields in separate steps, and checking after only the first would false-positive
        on the MZ_DEFAULT_ACTIVITY_HOUR fallback before the real time is even applied).

        Meeting activities are skipped here - their date_deadline is just a placeholder
        until the calendar event is attached, which is checked separately in
        calendar_event.py against the event's real start time.
        """
        now = fields.Datetime.now()
        for activity in self:
            if activity.res_model != 'crm.lead' or activity.activity_type_id.category == 'meeting':
                continue
            resolved = activity._mz_resolve_activity_datetime()
            if resolved and resolved < now:
                raise UserError(_(
                    "Can't schedule '%s' in the past. Pick a date/time that hasn't passed yet."
                ) % (activity.summary or activity.activity_type_id.name))
