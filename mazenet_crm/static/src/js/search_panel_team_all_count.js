/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { SearchModel } from "@web/search/search_model";

// CRM Pipeline's Sales Team search panel (crm_lead_views.xml's searchpanel on team_id) shows a
// per-team count next to each team, but stock Odoo's search panel widget never gives the built-in
// "All" entry a count at all - it's hardcoded with no __count property
// (addons/web/static/src/search/search_arch_parser.js, section.values.set(false, {...})), and the
// template only renders a counter when one exists (search_panel.xml: "section.enableCounters and
// value.__count gt 0"). Client asked for "All" to show the overall total too - patched here rather
// than in a view/XML attribute since this is a hardcoded gap in the widget itself, not something
// enable_counters or any arch attribute controls.
//
// Scoped to crm.lead's own team_id category only (not every category search panel app-wide) so
// this doesn't change behavior anywhere else in Odoo the client hasn't asked about.
patch(SearchModel.prototype, {
    _createCategoryTree(sectionId, result) {
        super._createCategoryTree(sectionId, result);
        const category = this.sections.get(sectionId);
        if (this.resModel !== "crm.lead" || category.fieldName !== "team_id" || !category.enableCounters) {
            return;
        }
        const allValue = category.values.get(false);
        if (!allValue) {
            return;
        }
        let total = 0;
        for (const [valueId, value] of category.values) {
            if (valueId !== false && typeof value.__count === "number") {
                total += value.__count;
            }
        }
        allValue.__count = total;
    },
});
