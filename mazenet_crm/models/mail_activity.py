# -*- coding: utf-8 -*-
from datetime import datetime, timedelta

import pytz

from odoo import models, fields

# Applied when an activity has neither a linked calendar event nor an explicit
# mz_activity_time - see crm_lead.py for why (keeps date-only activities from always
# sorting at midnight, ahead of every same-day timed activity).
MZ_DEFAULT_ACTIVITY_HOUR = 9.0


class MailActivity(models.Model):
    _inherit = "mail.activity"

    mz_activity_time = fields.Float(
        string="Time",
        widget="float_time",
        help="Time of day for this activity. Odoo only stores a time for Meeting-category "
             "activities (via the linked calendar.event) - Calls and To-Dos have no time "
             "anywhere in the database otherwise, which is why two same-day activities of "
             "those types can't be told apart for sorting. Ignored for Meeting-category "
             "activities, whose time comes from calendar_event_id.start instead."
    )

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
        naive_local = datetime.combine(self.date_deadline, datetime.min.time()) + timedelta(hours=hour)
        try:
            localized = pytz.timezone(tz_name).localize(naive_local)
        except pytz.exceptions.AmbiguousTimeError:
            localized = pytz.timezone(tz_name).localize(naive_local, is_dst=False)
        return localized.astimezone(pytz.UTC).replace(tzinfo=None)
