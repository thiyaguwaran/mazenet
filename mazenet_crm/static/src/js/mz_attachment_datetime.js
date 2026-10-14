/** @odoo-module **/
import { registry } from "@web/core/registry";
import { formatDateTime } from "@web/core/l10n/dates";
import {
    Many2ManyBinaryField,
    many2ManyBinaryField,
} from "@web/views/fields/many2many_binary/many2many_binary_field";

// many2many_binary + the upload date/time under each file name.
// The stamp is the attachment's own create_date (Odoo's magic field), so it is the real upload
// moment, server-set and shown in the viewer's timezone; no extra stored field is needed.
export class MzMany2ManyBinaryField extends Many2ManyBinaryField {
    formatUploadedAt(file) {
        return formatDateTime(file.create_date);
    }
}

registry.category("fields").add("mz_many2many_binary", {
    ...many2ManyBinaryField,
    component: MzMany2ManyBinaryField,
    relatedFields: [
        ...many2ManyBinaryField.relatedFields,
        { name: "create_date", type: "datetime" },
    ],
});
