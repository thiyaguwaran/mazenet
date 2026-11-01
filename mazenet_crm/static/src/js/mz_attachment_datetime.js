/** @odoo-module **/
import { registry } from "@web/core/registry";
import { formatDateTime } from "@web/core/l10n/dates";
import { user } from "@web/core/user";
import { onWillStart } from "@odoo/owl";
import {
    Many2ManyBinaryField,
    many2ManyBinaryField,
} from "@web/views/fields/many2many_binary/many2many_binary_field";

// many2many_binary + the upload date/time under each file name.
// The stamp is the attachment's own create_date and create_uid (Odoo's magic fields), so it is the
// real upload moment and uploader, server-set and shown in the viewer's timezone; no extra stored
// field is needed.
export class MzMany2ManyBinaryField extends Many2ManyBinaryField {
    // Attachments on these fields are permanent for everyone except CTO / Admin: the delete (x)
    // button only shows for that group, and the handler refuses for anyone else as a second guard.
    // crm.lead.write / ir.attachment.unlink enforce the same rule server-side.
    setup() {
        super.setup();
        this.isCtoAdmin = false;
        onWillStart(async () => {
            this.isCtoAdmin = await user.hasGroup("mazenet_access_rights.group_mzr_cto_admin");
        });
    }

    get canRemove() {
        return this.isCtoAdmin;
    }

    async onFileRemove(deleteId) {
        if (this.canRemove) {
            return super.onFileRemove(deleteId);
        }
    }

    formatUploadedAt(file) {
        return formatDateTime(file.create_date);
    }

    uploadedBy(file) {
        return file.create_uid && file.create_uid.display_name;
    }
}

registry.category("fields").add("mz_many2many_binary", {
    ...many2ManyBinaryField,
    component: MzMany2ManyBinaryField,
    relatedFields: [
        ...many2ManyBinaryField.relatedFields,
        { name: "create_date", type: "datetime" },
        { name: "create_uid", type: "many2one", relation: "res.users" },
    ],
});
