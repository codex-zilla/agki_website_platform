# AGKI Website CMS — Design Doc

**Covers:** turning the marketing site (`app/`, `app/templates/app/home.html`) from hardcoded HTML into staff-editable content, without a developer touching code for routine updates.
**Status:** Draft for review

---

## 1. What we're actually solving

I re-read `home.html` (1,515 lines) end to end. It's one long, well-built static page with ~14 distinct content sections, all hardcoded: hero copy, about/mission/vision, 4 core values, 3 "problem" cards, 3 problem stats, 6 service rows, 6 "why us" rows, 8 award logos, 2 testimonial videos, 6 community photos, 3 news cards, 4 FAQ items, 9 team members, and footer contact details. Every one of those currently requires editing a Django template and redeploying to change.

Two things worth flagging up front because they change the design, found while reviewing `app/` for this task:

1. **The contact form doesn't go anywhere.** `home.html`'s `<form class="modern-form">` has no `action`, and the page's own JS does `e.preventDefault()` and shows a fake `alert('Thank you! We will respond soon.')` — identical pattern to the fake "success" flows found in the appointments audit. Nobody at AGKI ever receives these messages. Fixed as part of this design (§6).
2. **`app/models.py`'s `Appointment`, and the `book_appointment` / `appointment_success` / `signin` views + templates, are dead code.** The real booking flow is `appointments/`; the real sign-in is `useraccount/`. Both `app.urls` and `useraccount.urls` register a URL named `signin` — because `app.urls` is included first in `project/urls.py` and `useraccount.urls` second, Django's `{% url 'signin' %}` resolves to `useraccount`'s (the working one), and `app`'s own `/signin/` page — which has no POST handling at all — is simply unreachable via any link on the site. This is pre-existing confusion, not something the CMS work causes, but it's the same "app" directory I'm reorganizing, so I'm flagging it now: recommend deleting `app.Appointment`, its three views, three templates, and the duplicate `signin` URL as a small separate cleanup PR (§7.5) — not required for the CMS to ship, but doing the CMS work in this directory without mentioning it would be an oversight.

---

## 2. Approach: Django Admin, not a bespoke editor

Both prior audits (`appointments/`, `doctorDashboard/`) found the same failure pattern: hand-rolled staff dashboards with no auth wall, fake buttons, and mismatched fields, because someone hand-built a UI instead of using a framework that already solved those problems. I'm not repeating that here.

**Recommendation: Django's built-in admin app**, customized, as the editing interface. Reasons:

- Auth, permissions, and per-model change history (`LogEntry` — who changed what, when) come free and battle-tested — exactly the auth layer that was missing in `doctorDashboard`.
- Zero custom frontend to build, test, or secure. A content editor gets a working, permissioned CRUD UI the moment a model is registered.
- Time-to-ship: this is a marketing site for a lab, not a product — a bespoke "visual page builder" is a multi-month investment this doesn't justify.

Three small add-ons make it non-technical-friendly without turning it into a custom build:

- **`django-ckeditor-5`** — rich text widget for the handful of fields that need bold/links/paragraphs (about copy, news body, FAQ answers), so staff aren't hand-writing HTML.
- **`django-admin-sortable2`** — drag-and-drop reordering for the many "list of cards" models (team, services, news, FAQ, testimonials) instead of typing order numbers.
- Admin branding (`admin.site.site_header = "AGKI Content Manager"`, custom `index_title`) — five minutes of work that stops it looking like a generic Django admin to non-technical staff.

---

## 3. Codebase organization: a new `cms` app

Don't bolt content models onto `app/` — it already conflates the live homepage with the dead legacy code from §1, and mixing "presentation" with "content authoring" makes both harder to reason about. New app:

```
cms/
  models.py       # every content model in §4
  admin.py        # ModelAdmin registrations, thumbnails, sortable config
  apps.py
  migrations/
  management/
    commands/
      seed_home_content.py   # one-time import of today's hardcoded copy (§7.2)
```

`app/` keeps: `home` view (now content-driven — imports `cms` models, builds context), `app/urls.py`, `home.html` (rewritten to loop over context instead of hardcoding). `app/models.py`, and the three dead views/templates from §1, go away in the cleanup pass.

Why a separate app and not folding into an existing one: `cms` has zero dependency on `appointments`, `doctorDashboard`, or `useraccount` — it's purely "content that the homepage renders." Keeping it standalone means the content-editor permission group (§5) can be scoped to exactly this app with no risk of leaking into patient or staff-account data.

---

## 4. Data model

Two shapes: **singletons** (one row, edited like a settings form — hero, about, problem intro) and **repeatable/orderable lists** (team members, news posts, etc). All list models get `order = PositiveIntegerField(default=0)` managed via `adminsortable2`, and an `is_published = BooleanField(default=True)` where staging-without-deleting matters.

### 4.1 Singletons

```python
class SiteSettings(models.Model):
    """Enforced as a single row — see SingletonModel pattern below."""
    site_name = models.CharField(max_length=100, default="AGKI Medical Lab")
    tagline = models.CharField(max_length=150, blank=True)          # "AI powered · Solar powered"
    logo = models.ImageField(upload_to="cms/site/", blank=True)
    favicon = models.ImageField(upload_to="cms/site/", blank=True)
    phone = models.CharField(max_length=30)
    email = models.EmailField()
    address_line = models.CharField(max_length=150)                 # "Kibaha, Pwani"
    facebook_url = models.URLField(blank=True)
    twitter_url = models.URLField(blank=True)
    instagram_url = models.URLField(blank=True)
    linkedin_url = models.URLField(blank=True)
    google_analytics_id = models.CharField(max_length=30, blank=True)
    map_embed_url = models.URLField(blank=True)
    footer_copyright_text = models.CharField(max_length=200, blank=True)

class HeroContent(models.Model):
    kicker_text = models.CharField(max_length=60)                   # "Afya Mfukoni"
    headline_line1 = models.CharField(max_length=60)                # "Healthcare That"
    headline_line2 = models.CharField(max_length=60)                # "Reaches Every"
    headline_accent = models.CharField(max_length=60)                # "Community" (styled differently)
    lede = models.TextField()
    primary_cta_text = models.CharField(max_length=40)
    primary_cta_url = models.CharField(max_length=200)
    secondary_cta_text = models.CharField(max_length=40, blank=True)
    secondary_cta_url = models.CharField(max_length=200, blank=True)

class HeroImpactStat(models.Model):   # repeatable — "500,000+ Patients served" etc
    hero = models.ForeignKey(HeroContent, on_delete=models.CASCADE, related_name="impact_stats")
    icon_class = models.CharField(max_length=60)       # "fa-solid fa-users"
    number = models.CharField(max_length=20)            # stored as text: "500000" or "24/7"
    suffix = models.CharField(max_length=10, blank=True) # "+"
    label = models.CharField(max_length=60)
    order = models.PositiveIntegerField(default=0)

class AboutContent(models.Model):
    story_badge = models.CharField(max_length=40)        # "Est. 2022"
    story_heading = models.CharField(max_length=150)
    story_copy = models.TextField()                       # CKEditor
    mission_text = models.TextField()
    vision_text = models.TextField()
    youtube_embed_url = models.URLField(blank=True)

class CoreValue(models.Model):        # repeatable — "Compassion / fa-heart" etc
    about = models.ForeignKey(AboutContent, on_delete=models.CASCADE, related_name="core_values")
    icon_class = models.CharField(max_length=60)
    label = models.CharField(max_length=40)
    order = models.PositiveIntegerField(default=0)

class ProblemSectionContent(models.Model):
    eyebrow = models.CharField(max_length=80)
    heading = models.CharField(max_length=150)
    lede = models.TextField()
    cta_text = models.CharField(max_length=60, blank=True)
    cta_url = models.CharField(max_length=200, blank=True)
```

### 4.2 Repeatable content

```python
class ProblemCard(models.Model):
    image = models.ImageField(upload_to="cms/problem/")
    image_alt = models.CharField(max_length=150)
    icon_class = models.CharField(max_length=60)
    title = models.CharField(max_length=150)
    body = models.TextField()          # CKEditor — was 2 <p> tags, one rich field is fine
    order = models.PositiveIntegerField(default=0)

class ProblemStat(models.Model):
    icon_class = models.CharField(max_length=60)
    number = models.CharField(max_length=20)     # "75%", "Millions" — text, not numeric, on purpose
    label = models.CharField(max_length=100)
    description = models.CharField(max_length=200)
    order = models.PositiveIntegerField(default=0)

class FeatureRow(models.Model):
    """Powers BOTH 'Our Solutions' and 'Why Choose AGKI' — same shape, different section."""
    class Category(models.TextChoices):
        SOLUTION = "solution", "Our Solutions"
        WHY_US = "why_us", "Why Choose AGKI"
    category = models.CharField(max_length=20, choices=Category.choices)
    icon_class = models.CharField(max_length=60)
    title = models.CharField(max_length=100)
    description = models.CharField(max_length=200)
    order = models.PositiveIntegerField(default=0)

class AwardLogo(models.Model):
    class Size(models.TextChoices):
        NORMAL = "normal", "Normal"
        LARGE = "lg", "Large"
        XLARGE = "xl", "Extra large"
    image = models.ImageField(upload_to="cms/awards/")
    alt_text = models.CharField(max_length=100)
    size = models.CharField(max_length=10, choices=Size.choices, default=Size.NORMAL)
    order = models.PositiveIntegerField(default=0)

class Testimonial(models.Model):
    patient_name = models.CharField(max_length=100, blank=True)   # optional attribution
    # Both fields are optional individually — `clean()` requires at least one.
    # (An earlier draft made youtube_url required, which contradicted video_file
    # being an "escape hatch": a staffer uploading a file instead of a URL would
    # have been blocked by the required URL field. Caught during implementation.)
    youtube_url = models.URLField(blank=True, help_text="Preferred: unlisted YouTube/Vimeo link — see §6.2")
    video_file = models.FileField(upload_to="cms/testimonials/", blank=True, null=True)
    is_published = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    def clean(self):
        if not self.youtube_url and not self.video_file:
            raise ValidationError("Provide either a YouTube/Vimeo URL or an uploaded video file.")

class CommunityPhoto(models.Model):
    image = models.ImageField(upload_to="cms/community/%Y/%m/")
    caption = models.CharField(max_length=150)
    is_published = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

class NewsPost(models.Model):
    image = models.ImageField(upload_to="cms/news/%Y/%m/")
    title = models.CharField(max_length=150)
    slug = models.SlugField(unique=True)
    excerpt = models.CharField(max_length=250)
    body = models.TextField(blank=True)     # CKEditor — for a future full article page
    published_date = models.DateField()
    is_published = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

class FAQItem(models.Model):
    icon_class = models.CharField(max_length=60)
    question = models.CharField(max_length=150)
    answer = models.TextField()   # CKEditor
    is_published = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

class TeamMember(models.Model):
    photo = models.ImageField(upload_to="cms/team/")
    name = models.CharField(max_length=100)
    role = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)   # unpublish without deleting a person's record
    order = models.PositiveIntegerField(default=0)

class ContactMessage(models.Model):
    """NEW — fixes the contact form that currently goes nowhere (§1, §6)."""
    name = models.CharField(max_length=100)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True)
    subject = models.CharField(max_length=100)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)
```

### 4.3 Singleton enforcement

`SiteSettings`, `HeroContent`, `AboutContent`, `ProblemSectionContent` should only ever have one row. Rather than a third-party package, override `save()`:

```python
class SingletonModel(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
```

Admin for these registers as a single "change" page (no add/delete), same pattern Django uses for things like site config — familiar to anyone who's used Django admin before.

---

## 5. Permissions

Create a **"Content Editors"** Django `Group` with `add`/`change`/`delete`/`view` permissions scoped to every model in `cms` only — nothing in `appointments`, `doctorDashboard`, or `useraccount`. Staff who only need to update the homepage get an account in this group, **not** `is_staff`+`is_superuser` blanket access. This is the least-privilege pattern the `doctorDashboard` audit found completely absent (zero auth, let alone role scoping) — the CMS should be the example other parts of this codebase eventually follow, not another exception.

`ContactMessage` is read/view-mostly for content editors (they shouldn't need delete — keep a full inbound-message record); give delete permission only to admins.

---

## 6. Fixing the contact form (found during this review)

Real view, real persistence, no client-side fake success:

```python
# app/forms.py (or cms/forms.py)
class ContactForm(forms.ModelForm):
    class Meta:
        model = ContactMessage
        fields = ["name", "email", "phone", "subject", "message"]

# app/views.py
def home(request):
    if request.method == "POST" and "message" in request.POST:
        form = ContactForm(request.POST)
        if form.is_valid():
            form.save()
            send_mail(  # reuses the SMTP config already used by useraccount's OTP emails
                subject=f"New contact form message: {form.cleaned_data['subject']}",
                message=form.cleaned_data["message"],
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[settings.DEFAULT_FROM_EMAIL],
                fail_silently=True,
            )
            messages.success(request, "Thank you — we'll respond soon.")
            return redirect(f"{reverse('home')}#contact")
    else:
        form = ContactForm()
    return render(request, "app/home.html", {..., "contact_form": form})
```

Drop the JS `e.preventDefault()` fake-alert handler; let the form submit normally with a `{% csrf_token %}`, same pattern already correctly used elsewhere in this codebase (booking forms, dashboard approval forms).

### 6.2 Why testimonials should prefer a YouTube/Vimeo URL over uploaded video files

`passenger_wsgi.py` in the repo root indicates this runs on shared/Passenger hosting, not something like S3-backed storage. The current `mama.mp4` / `testmony2.mp4` are static files checked into git and served directly by the app — fine for two videos, not something to scale as a content-editable feature (staff uploading new testimonial videos through Django admin means every video's bytes flow through the web server's disk and bandwidth). The About section already successfully embeds a YouTube video — same pattern extends cleanly to testimonials. `Testimonial.video_file` stays as an escape hatch (kept in the model) for a video someone can't/won't put on YouTube, but the admin form and help text steer staff toward the URL field by default.

---

## 7. Media handling & migration plan

### 7.1 Folder layout

```
media/
  cms/
    site/            # logo, favicon
    team/
    news/2026/03/     # date-partitioned — avoids one folder with hundreds of files over time
    community/2026/03/
    testimonials/
    awards/
    problem/
```

`MEDIA_ROOT`/`MEDIA_URL` are already correctly configured in `settings.py` — no new infrastructure needed, just per-model `upload_to` paths as shown in §4.

### 7.2 One-time seed: import today's hardcoded content

A management command, `python manage.py seed_home_content`, populates every model in §4 from the copy that's live in `home.html` right now (I extracted it in full while reviewing the file — hero copy, all 9 team members with roles, all 6 service rows, all 6 why-us rows, all 4 FAQ items, all 3 news cards, etc.) and copies the referenced files from `app/static/app/*.{jpg,png,jpeg,mp4}` into the new `media/cms/...` paths, saving the resulting relative path on each row. This means **cutover day requires zero re-typing** — staff inherit the exact current site, then edit from there. Idempotent (safe to re-run against a clean DB), and doubles as living documentation of what the pre-CMS content was.

### 7.3 Upload validation

Every `ImageField`/`FileField` gets:

- `FileExtensionValidator` (jpg/jpeg/png/webp for images; mp4/webm for the video escape hatch)
- A max-size validator (Django has no built-in one — a small `validators.py` with a `MaxFileSizeValidator(5 * 1024 * 1024)` for images, larger for the video field)

### 7.4 Backup implication (operationally important)

Before this change, every content image was a **git-tracked static file** — reproducible from the repo, backed up implicitly by version control. After this change, content images live in the database (paths) and `media/` (bytes), **outside git**. This is the correct tradeoff (that's the whole point — editable without a deploy) but it means `media/` now needs its own backup story: a periodic (nightly) `media/` + `db.sqlite3`/database dump backup job, since losing that directory now means losing content that git no longer has a copy of. Flagging this explicitly so it isn't discovered the hard way.

### 7.5 Recommended companion cleanup (separate PR, not blocking)

Delete `app.Appointment` model + migration history, `book_appointment`/`appointment_success`/`signin` views, their 3 templates, and the corresponding `app/urls.py` entries — all dead/shadowed code identified in §1. Low risk (nothing links to them), but keep it as its own commit so a `git blame`/revert story stays clean and separate from the CMS feature.

---

## 8. Template changes

`home.html` keeps its exact HTML/CSS structure (no reason to touch `home.css` or the AOS/scroll-spy JS) — every hardcoded block becomes a loop or variable pull:

```django
{# before #}
<strong class="hero-impact-number" data-count-to="500000" data-suffix="+">500,000+</strong>

{# after #}
{% for stat in hero.impact_stats.all %}
<div class="hero-impact-item">
    <span class="hero-impact-icon"><i class="{{ stat.icon_class }}"></i></span>
    <strong class="hero-impact-number" data-count-to="{{ stat.number }}" data-suffix="{{ stat.suffix }}">{{ stat.number }}{{ stat.suffix }}</strong>
    <span class="hero-impact-label">{{ stat.label }}</span>
</div>
{% endfor %}
```

Every section wraps in `{% if %}` so an emptied-out table degrades gracefully instead of rendering a broken/empty-looking block — e.g. `{% if team_members %}<section id="team">...{% endif %}`. A content editor deleting every team member should make the Team section disappear cleanly, not 500 or show a blank scrolling carousel.

`app/views.py::home` builds the context:

```python
def home(request):
    context = {
        "settings": SiteSettings.load(),
        "hero": HeroContent.load(),
        "about": AboutContent.load(),
        "problem": ProblemSectionContent.load(),
        "problem_cards": ProblemCard.objects.order_by("order"),
        "problem_stats": ProblemStat.objects.order_by("order"),
        "solutions": FeatureRow.objects.filter(category="solution").order_by("order"),
        "why_us": FeatureRow.objects.filter(category="why_us").order_by("order"),
        "awards": AwardLogo.objects.order_by("order"),
        "testimonials": Testimonial.objects.filter(is_published=True).order_by("order"),
        "community_photos": CommunityPhoto.objects.filter(is_published=True).order_by("order"),
        "news_posts": NewsPost.objects.filter(is_published=True).order_by("-published_date"),
        "faqs": FAQItem.objects.filter(is_published=True).order_by("order"),
        "team_members": TeamMember.objects.filter(is_active=True).order_by("order"),
        "contact_form": form,   # from §6
    }
    return render(request, "app/home.html", context)
```

---

## 9. Performance: caching

The homepage goes from 0 DB queries (static template) to ~14. Fine for a marketing site's traffic, but cheap to guard anyway:

- `@cache_page(60 * 10)` (10 minutes) on the `home` view is the pragmatic default — content edits show up within 10 minutes, no cache-invalidation machinery needed.
- If staff want edits to appear instantly, upgrade later to signal-based invalidation (`post_save`/`post_delete` on every `cms` model → `cache.delete(...)` the homepage key) — noted as a Phase 2 option, not built up front since it adds real complexity for a benefit (instant vs. 10-minute staleness) that likely doesn't matter for this content.
- The contact form POST can't sit behind `cache_page` as-is (it needs to run on every request) — split into `home` (cached, GET) + a separate `contact_submit` POST-only view that redirects back to `home#contact`, keeping the cache simple.

---

## 10. Testing

Given both prior audits found `tests.py` empty everywhere in this codebase, the CMS ships with a real minimum bar:

- `SingletonModel.load()` never creates a second row under concurrent calls.
- `home` view renders 200 with **zero** rows in every `cms` table (graceful empty-state — the most important test given the "degrades gracefully" design goal in §8).
- `home` view renders 200 and shows seeded content after `seed_home_content` runs.
- Content-editor group membership: can reach `cms` admin pages, gets 403 on `appointments`/`doctorDashboard`/`useraccount` admin pages.
- `ContactMessage` is actually created on a valid POST to the contact form, and `send_mail` is called (mocked in test).

---

## 11. Rollout order

1. Build `cms` app: models, migrations, admin registrations (with thumbnails, sortable, singleton pattern).
2. Add `cms` to `INSTALLED_APPS`, create the "Content Editors" group + permissions.
3. Run `seed_home_content` against a copy of production data — verify the seeded admin content matches the live site exactly.
4. Rewrite `home.html` + `app/views.py::home` to be content-driven; keep the old hardcoded version reachable on a branch until the new one is verified pixel-identical.
5. Wire the contact form (§6) — independent of the rest, can ship first or last.
6. Add `cache_page` once the above is stable.
7. (Separate PR) delete the dead code from §7.5.
8. Hand off: create the first real Content Editor account for AGKI staff, walk them through adding one news post and reordering the team carousel as acceptance testing.
