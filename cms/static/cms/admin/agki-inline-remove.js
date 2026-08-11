/*
 * A single "Remove" affordance on every inline row.
 *
 * Django renders two different delete controls in a tabular inline:
 * unsaved rows get a "Remove" link (added by admin/js/inlines.js, which
 * deletes the row from the DOM outright), while *saved* rows get a
 * "Delete?" checkbox — see the comment on addInlineDeleteButton() in
 * django/contrib/admin/static/admin/js/inlines.js. The approved design
 * shows one red "Remove" on every row, so this script gives saved rows
 * the same affordance.
 *
 * Important: it must NOT remove a saved row from the DOM the way
 * inlines.js does for unsaved ones. The formset's management form still
 * counts that row in TOTAL_FORMS, so dropping its inputs from the POST
 * would fail formset validation. Instead the row's existing DELETE
 * checkbox is ticked and the row is hidden — its inputs still submit,
 * and Django deletes the row on save exactly as if the checkbox had been
 * ticked by hand. Nothing is deleted until the form is saved.
 */
(function () {
    "use strict";

    // Django publishes the inline's own localised strings on the wrapper
    // as JSON, so the link text stays translated rather than hardcoded.
    function removeLabel(group) {
        try {
            return JSON.parse(group.dataset.inlineFormset).options.deleteText || "Remove";
        } catch (err) {
            return "Remove";
        }
    }

    function enhance(group) {
        var label = removeLabel(group);
        var checkboxes = group.querySelectorAll('td.delete input[type="checkbox"][name$="-DELETE"]');

        Array.prototype.forEach.call(checkboxes, function (checkbox) {
            var cell = checkbox.parentNode;
            if (cell.querySelector(".agki-remove-link")) {
                return;
            }
            checkbox.classList.add("agki-delete-checkbox");

            var link = document.createElement("a");
            link.href = "#";
            link.className = "inline-deletelink agki-remove-link";
            link.textContent = label;
            link.addEventListener("click", function (event) {
                event.preventDefault();
                checkbox.checked = true;
                var row = checkbox.closest("tr");
                if (row) {
                    row.classList.add("agki-row-removed");
                }
            });
            cell.appendChild(link);
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        Array.prototype.forEach.call(document.querySelectorAll(".inline-group"), enhance);
    });
})();
