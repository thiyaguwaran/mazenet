/** @odoo-module **/
import { Component, onMounted, onWillUnmount, reactive, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { patch } from "@web/core/utils/patch";
import { fields } from "@mail/core/common/record";
import { Activity } from "@mail/core/web/activity";
import { Activity as ActivityModel } from "@mail/core/common/activity_model";
import { standardWidgetProps } from "@web/views/widgets/standard_widget_props";

// Live countdown to an activity's due moment, then to the RED lock after it.
// The server only decides the lock (cron, every 2 min); this is display-only and counts in
// the browser, so it can be off by the viewer's clock skew and the cron's polling delay.
//
// One shared 1s ticker for every countdown on the page (a Pipeline kanban can show
// hundreds of cards) - started by the first mounted countdown, stopped by the last.
const ticker = reactive({ now: Date.now() });
let tickerTimer = null;
let tickerUsers = 0;

function useTicker() {
    const state = useState(ticker);
    onMounted(() => {
        tickerUsers++;
        ticker.now = Date.now();
        if (!tickerTimer) {
            tickerTimer = setInterval(() => {
                ticker.now = Date.now();
            }, 1000);
        }
    });
    onWillUnmount(() => {
        tickerUsers--;
        if (tickerUsers <= 0) {
            clearInterval(tickerTimer);
            tickerTimer = null;
            tickerUsers = 0;
        }
    });
    return state;
}

const pad = (n) => String(n).padStart(2, "0");

export function formatDuration(totalSeconds) {
    const s = Math.max(0, Math.floor(totalSeconds));
    const days = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    const clock = `${pad(h)}:${pad(m)}:${pad(s % 60)}`;
    return days ? `${days}d ${clock}` : clock;
}

export class MzCountdown extends Component {
    static template = "mazenet_crm.MzCountdown";
    static props = {
        due: { optional: true }, // luxon DateTime, or falsy when there's no activity
        graceMinutes: { type: Number, optional: true },
        hasRedLock: { type: Boolean, optional: true },
        locked: { type: Boolean, optional: true },
    };

    setup() {
        this.ticker = useTicker();
    }

    get info() {
        const { due, hasRedLock, locked } = this.props;
        if (locked) {
            return { cls: "mz-cd-red", icon: "fa-lock", text: "RED LOCKED" };
        }
        if (!due) {
            return null;
        }
        const diff = Math.floor((due.toMillis() - this.ticker.now) / 1000);
        if (diff > 0) {
            const cls = diff > 30 * 60 ? "mz-cd-green" : diff > 10 * 60 ? "mz-cd-yellow" : "mz-cd-orange";
            return { cls, icon: "fa-clock-o", text: `Due in ${formatDuration(diff)}` };
        }
        const over = -diff;
        if (!hasRedLock) {
            return { cls: "mz-cd-red", icon: "fa-exclamation-circle", text: `Overdue ${formatDuration(over)}` };
        }
        const left = (this.props.graceMinutes || 0) * 60 - over;
        if (left > 0) {
            return {
                cls: "mz-cd-orange",
                icon: "fa-hourglass-half",
                text: `Overdue ${formatDuration(over)} · locks in ${formatDuration(left)}`,
            };
        }
        return { cls: "mz-cd-red", icon: "fa-lock", text: `Overdue ${formatDuration(over)} · locking` };
    }
}

// --- view widget (kanban card + form) -----------------------------------------------------
export class MzCountdownWidget extends Component {
    static template = "mazenet_crm.MzCountdownWidget";
    static components = { MzCountdown };
    static props = { ...standardWidgetProps };

    get data() {
        return this.props.record.data;
    }
}

registry.category("view_widgets").add("mz_countdown", {
    component: MzCountdownWidget,
    fieldDependencies: [
        { name: "x_next_activity_datetime", type: "datetime" },
        { name: "x_grace_minutes", type: "integer" },
        { name: "x_has_red_lock", type: "boolean" },
        { name: "x_is_locked", type: "boolean" },
    ],
});

// --- view widget for mail.activity records (Activity Overview list) ------------------------
export class MzActivityCountdownWidget extends Component {
    static template = "mazenet_crm.MzActivityCountdownWidget";
    static components = { MzCountdown };
    static props = { ...standardWidgetProps };

    get data() {
        return this.props.record.data;
    }
}

registry.category("view_widgets").add("mz_activity_countdown", {
    component: MzActivityCountdownWidget,
    fieldDependencies: [
        { name: "mz_due_datetime", type: "datetime" },
        { name: "mz_grace_minutes", type: "integer" },
        { name: "mz_has_red_lock", type: "boolean" },
        { name: "active", type: "boolean" },
    ],
});

// --- chatter: one countdown per activity --------------------------------------------------
patch(ActivityModel.prototype, {
    setup() {
        super.setup(...arguments);
        this.mz_due_datetime = fields.Datetime();
        this.mz_has_red_lock = fields.Attr(false);
        this.mz_grace_minutes = fields.Attr(0);
        this.mz_is_lock_notice = fields.Attr(false);
    },
});

patch(Activity, {
    components: { ...Activity.components, MzCountdown },
});
