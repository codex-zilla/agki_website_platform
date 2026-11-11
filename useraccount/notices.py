"""User-facing sentences about why an account cannot be used.

Here rather than at the two places that say them. `signin` and
`AccountAdapter.pre_login` both explain the same refusal — the site's own form
and allauth's login path — and before plan 10.5 they held **two copies of the
same string**, which is how both came to be wrong at once when decision F
changed what the flag means.
"""

# Decision F, plan 10.5. This used to read "Your account is pending admin
# approval. You will be able to sign in once an administrator approves it."
#
# That sentence was true while `is_approved` meant "an administrator has not
# got to this account yet". It now means "an administrator switched this
# account off" (design §4.5), and nobody who can see this message is waiting
# for anything — so telling them to wait is telling them to wait for something
# that will never happen, and the more patient they are the longer they are
# stranded.
#
# It deliberately does not say *why* access was withdrawn. That is between the
# account holder and AGKI, and a login page is the wrong place to publish an
# administrator's reason to whoever is typing.
ACCESS_WITHDRAWN = (
    "Access to this account has been withdrawn, so it can no longer be used to "
    "sign in. If you think this is a mistake, please contact AGKI Medical "
    "Laboratory."
)

# A `NOT_VERIFIED` constant sat here: "Your account is not verified yet.
# Please check your email for OTP verification." It was another dead end of
# the same shape as the one above — it told somebody to go and find an email
# holding a code that expires in two minutes, at a moment that is by
# definition later than that, and nothing on the page led anywhere.
#
# `signin` now sends a stalled registration to the verification page with a
# fresh code, so the sentence it needs is about *this* address and cannot be
# a constant. `ACCESS_WITHDRAWN` above covers the other reading of an inactive
# account — one an administrator switched off — because to the person reading
# it, "approval withdrawn" and "account switched off" are the same news.
