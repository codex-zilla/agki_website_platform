"""Public-facing forms for the cms app.

Currently just the contact form, which replaces the homepage form that was
found during the design review to go nowhere at all — no `action`, no `name`
attributes on four of its five inputs, and a JS handler that called
`preventDefault()` and showed a fake "Thank you!" alert. See design doc §1/§6.
"""

from django import forms

from .models import ContactMessage

# The options offered by the homepage's custom "Subject" dropdown. Kept here
# rather than in the template so the list exists in one place, and so the
# rendered <li>s and the submitted value can't drift apart.
#
# Deliberately NOT enforced as `choices` on the field: the value arrives from a
# hidden input, and rejecting anything outside this list would turn a harmless
# unexpected value into a failed submission — losing a real message from a real
# person over a label. `subject` is only ever displayed to staff.
CONTACT_SUBJECTS = [
    "General Inquiry",
    "Partnership Opportunity",
    "Support Request",
    "Feedback",
    "Other",
]


class ContactForm(forms.ModelForm):
    class Meta:
        model = ContactMessage
        fields = ["name", "email", "phone", "subject", "message"]

    def clean_subject(self):
        """Collapse any newlines out of the subject.

        `subject` reaches us from a hidden input driven by the page's custom
        dropdown, so a hand-crafted POST can put anything in it — including
        newlines. That value goes on to become part of the notification
        email's Subject header, and Django rejects newlines there with
        `BadHeaderError` (its header-injection guard), which would surface as
        a 500 rather than a validation message. Normalising here keeps the
        submission working and the header safe.
        """
        return " ".join(self.cleaned_data["subject"].split())

    def clean_name(self):
        return " ".join(self.cleaned_data["name"].split())
