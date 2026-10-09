# -*- coding: utf-8 -*-
from odoo import models


class IrActionsActWindow(models.Model):
    _inherit = 'ir.actions.act_window'

    # CTO/Admin, MD and Corporate BU Manager: no create access at all (client
    # instruction, 2026-09-21 - "no need create lead access... hide the New
    # button for them"; Corporate BU Manager added same day, same reasoning).
    # Maps each ORIGINAL crm.lead view id (whatever a given action would
    # normally resolve to) to the matching create="false" replacement
    # (views/crm_lead_views.xml) - one per base view, since "New Lead /
    # Source" opportunities and plain leads use genuinely different base list
    # views (crm_case_tree_view_oppor vs crm_case_tree_view_leads), not one
    # shared list. The xmlids below still say "cto_md" for historical reasons
    # - not renamed, since that's pure churn with no functional effect.
    _MZ_NO_CREATE_VIEW_MAP = {
        'crm.crm_case_kanban_view_leads': 'mazenet_crm.mz_crm_lead_kanban_no_create_cto_md',
        'crm.crm_case_tree_view_oppor': 'mazenet_crm.mz_crm_lead_list_no_create_cto_md_oppor',
        'crm.crm_case_tree_view_leads': 'mazenet_crm.mz_crm_lead_list_no_create_cto_md_leads',
        'crm.crm_lead_view_form': 'mazenet_crm.mz_crm_lead_form_no_create_cto_md',
    }

    def _get_action_dict(self):
        """Swaps every crm.lead view in this action's 'views' list for its
        create="false" counterpart, for CTO/Admin, MD and Corporate BU Manager
        only - see _MZ_NO_CREATE_VIEW_MAP. This is the generic hook
        _for_xml_id() and the web client's own /web/action/load both go
        through, so it covers the Pipeline kanban, the Leads list, the
        Opportunities list, and the lead form's own New button uniformly from
        one place, unlike DMT's own dedicated-action redirect (crm_team.py's
        action_your_pipeline) which only ever applies to the one action it
        explicitly returns.

        Corporate BU Manager added 2026-09-21 (client instruction, same day as
        the original CTO/Admin/MD one, same reasoning - corp.mgr's own
        cross-team oversight role over Hunter/AM/Corp Training/LMS/TNH mirrors
        CTO/Admin/MD's rather than a normal single-team manager's). Keep this
        group list in sync with crm_lead.py's create() override, which has the
        exact same three-group check.

        ir.rule/ir.model.access.csv can't do this access block themselves -
        CTO/Admin qualifies for perm_create=True through dozens of other teams'
        own rules via implied_ids, and all three groups hold base.group_user,
        whose own crm.lead ACL row already grants create=1 model-wide; none
        can be "subtracted" from for one subgroup. The actual access block is
        crm.lead's own create() override - this only ever hides the button."""
        result = super()._get_action_dict()
        if result.get('res_model') != 'crm.lead' or not result.get('views'):
            return result
        user = self.env.user
        if not (
            user.has_group('mazenet_access_rights.group_mzr_cto_admin')
            or user.has_group('mazenet_access_rights.group_mzr_md')
            or user.has_group('mazenet_access_rights.group_mzr_corporate_manager')
        ):
            return result
        replacements = {}
        for base_xmlid, no_create_xmlid in self._MZ_NO_CREATE_VIEW_MAP.items():
            base_view = self.env.ref(base_xmlid, raise_if_not_found=False)
            no_create_view = self.env.ref(no_create_xmlid, raise_if_not_found=False)
            if base_view and no_create_view:
                replacements[base_view.id] = no_create_view.id
        # 'form' is False (no explicit view id pinned; "use whatever the default
        # form view resolves to") in every crm.lead action's 'views' list here -
        # substitute the actual default form view's own id first, so the same
        # replacements lookup below can catch it too (2026-09-21 client bug
        # report: New button still visible on an existing lead's own form).
        default_form_view = self.env.ref('crm.crm_lead_view_form', raise_if_not_found=False)
        new_views = []
        for view_id, view_type in result['views']:
            if not view_id and view_type == 'form' and default_form_view:
                view_id = default_form_view.id
            new_views.append((replacements.get(view_id, view_id), view_type))
        result['views'] = new_views
        return result
