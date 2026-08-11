from django.core.exceptions import ValidationError
from django.db import models
from django_ckeditor_5.fields import CKEditor5Field

from cms.validators import (
    image_extension_validator,
    validate_image_file_size,
    validate_video_file_size,
    video_extension_validator,
)


class SingletonModel(models.Model):
    """Base for content blocks that only ever have one row (site settings,
    hero, about, problem-section intro). `load()` is the only supported way
    to fetch/create the row — always returns the same one.
    """

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # Singletons aren't meant to be deleted from the admin — there's
        # nothing sensible to render once they're gone. Keep the row and
        # let editors clear its fields instead if they want to blank it out.
        pass

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class SiteSettings(SingletonModel):
    site_name = models.CharField(max_length=100, default="AGKI Medical Lab")
    tagline = models.CharField(max_length=150, blank=True, help_text="Shown next to the logo, e.g. 'AI powered · Solar powered'")
    logo = models.ImageField(
        upload_to="cms/site/",
        blank=True,
        validators=[image_extension_validator, validate_image_file_size],
    )
    favicon = models.ImageField(
        upload_to="cms/site/",
        blank=True,
        validators=[image_extension_validator, validate_image_file_size],
    )

    phone = models.CharField(max_length=30)
    email = models.EmailField()
    address_line = models.CharField(max_length=150, help_text="e.g. 'Kibaha, Pwani'")

    facebook_url = models.URLField(blank=True)
    twitter_url = models.URLField(blank=True)
    instagram_url = models.URLField(blank=True)
    linkedin_url = models.URLField(blank=True)

    google_analytics_id = models.CharField(max_length=30, blank=True)
    map_embed_url = models.URLField(blank=True, help_text="Google Maps 'embed' iframe src URL")
    footer_copyright_text = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = "Site settings"
        verbose_name_plural = "Site settings"

    def __str__(self):
        return self.site_name


class HeroContent(SingletonModel):
    kicker_text = models.CharField(max_length=60, help_text="Small label above the headline, e.g. 'Afya Mfukoni'")
    headline_line1 = models.CharField(max_length=60, verbose_name="Headline line 1")
    headline_line2 = models.CharField(max_length=60, verbose_name="Headline line 2")
    headline_accent = models.CharField(max_length=60, help_text="Styled differently from the two lines above, e.g. 'Community'")
    lede = models.TextField()

    primary_cta_text = models.CharField(max_length=40, verbose_name="Primary CTA text")
    primary_cta_url = models.CharField(max_length=200, verbose_name="Primary CTA URL")
    secondary_cta_text = models.CharField(max_length=40, blank=True, verbose_name="Secondary CTA text")
    secondary_cta_url = models.CharField(max_length=200, blank=True, verbose_name="Secondary CTA URL")

    class Meta:
        verbose_name = "Hero section"
        verbose_name_plural = "Hero section"

    def __str__(self):
        return f"Hero: {self.headline_line1} {self.headline_line2} {self.headline_accent}"


class HeroImpactStat(models.Model):
    """Repeatable — the '500,000+ Patients served' style stats in the hero."""

    hero = models.ForeignKey(HeroContent, on_delete=models.CASCADE, related_name="impact_stats")
    icon_class = models.CharField(max_length=60, help_text="Font Awesome class, e.g. 'fa-solid fa-users'")
    number = models.CharField(max_length=20, help_text="Stored as text so non-numeric values like '24/7' work too")
    suffix = models.CharField(max_length=10, blank=True, help_text="e.g. '+'")
    label = models.CharField(max_length=60)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]
        verbose_name = "Hero impact stat"

    def __str__(self):
        return f"{self.number}{self.suffix} {self.label}"


class AboutContent(SingletonModel):
    story_badge = models.CharField(max_length=40, help_text="e.g. 'Est. 2022'")
    story_heading = models.CharField(max_length=150)
    story_copy = CKEditor5Field(config_name="default")
    mission_text = models.TextField()
    vision_text = models.TextField()
    youtube_embed_url = models.URLField(blank=True)

    class Meta:
        verbose_name = "About section"
        verbose_name_plural = "About section"

    def __str__(self):
        return self.story_heading or "About section"


class CoreValue(models.Model):
    """Repeatable — 'Compassion / fa-heart' style cards under About."""

    about = models.ForeignKey(AboutContent, on_delete=models.CASCADE, related_name="core_values")
    icon_class = models.CharField(max_length=60)
    label = models.CharField(max_length=40)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]
        verbose_name = "Core value"

    def __str__(self):
        return self.label


class ProblemSectionContent(SingletonModel):
    eyebrow = models.CharField(max_length=80)
    heading = models.CharField(max_length=150)
    lede = models.TextField()
    cta_text = models.CharField(max_length=60, blank=True)
    cta_url = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = "Problem section intro"
        verbose_name_plural = "Problem section intro"

    def __str__(self):
        return self.heading or "Problem section intro"


class ProblemCard(models.Model):
    image = models.ImageField(
        upload_to="cms/problem/",
        validators=[image_extension_validator, validate_image_file_size],
    )
    image_alt = models.CharField(max_length=150)
    icon_class = models.CharField(max_length=60)
    title = models.CharField(max_length=150)
    body = models.TextField()
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]
        verbose_name = "Problem card"

    def __str__(self):
        return self.title


class ProblemStat(models.Model):
    icon_class = models.CharField(max_length=60)
    number = models.CharField(max_length=20, help_text="Text, not numeric — e.g. '75%' or 'Millions'")
    label = models.CharField(max_length=100)
    description = models.CharField(max_length=200)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]
        verbose_name = "Problem stat"

    def __str__(self):
        return f"{self.number} — {self.label}"


class FeatureRow(models.Model):
    """Shared by 'Our Solutions' and 'Why Choose AGKI' — same card shape
    (icon + title + description), different section. `category` decides
    which section a row renders in.
    """

    class Category(models.TextChoices):
        SOLUTION = "solution", "Our Solutions"
        WHY_US = "why_us", "Why Choose AGKI"

    category = models.CharField(max_length=20, choices=Category.choices)
    icon_class = models.CharField(max_length=60)
    title = models.CharField(max_length=100)
    description = models.CharField(max_length=200)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["category", "order"]
        verbose_name = "Feature row"

    def __str__(self):
        return f"[{self.get_category_display()}] {self.title}"


class AwardLogo(models.Model):
    class Size(models.TextChoices):
        NORMAL = "normal", "Normal"
        LARGE = "lg", "Large"
        XLARGE = "xl", "Extra large"

    image = models.ImageField(
        upload_to="cms/awards/",
        validators=[image_extension_validator, validate_image_file_size],
    )
    alt_text = models.CharField(max_length=100)
    size = models.CharField(max_length=10, choices=Size.choices, default=Size.NORMAL)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]
        verbose_name = "Award logo"

    def __str__(self):
        return self.alt_text


class Testimonial(models.Model):
    patient_name = models.CharField(max_length=100, blank=True, help_text="Optional attribution.")
    youtube_url = models.URLField(
        blank=True,
        help_text="Preferred: unlisted YouTube/Vimeo link (see design doc §6.2). Leave blank only if uploading a video file below.",
    )
    video_file = models.FileField(
        upload_to="cms/testimonials/",
        blank=True,
        null=True,
        help_text="Escape hatch only — prefer the URL field above. Large uploads are costly on shared hosting.",
        validators=[video_extension_validator, validate_video_file_size],
    )
    is_published = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]
        verbose_name = "Testimonial"

    def __str__(self):
        return self.patient_name or f"Testimonial #{self.pk}"

    def clean(self):
        if not self.youtube_url and not self.video_file:
            raise ValidationError("Provide either a YouTube/Vimeo URL or an uploaded video file.")


class CommunityPhoto(models.Model):
    image = models.ImageField(
        upload_to="cms/community/%Y/%m/",
        validators=[image_extension_validator, validate_image_file_size],
    )
    caption = models.CharField(max_length=150)
    is_published = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]
        verbose_name = "Community photo"
        verbose_name_plural = "Community photos"

    def __str__(self):
        return self.caption


class NewsPost(models.Model):
    image = models.ImageField(
        upload_to="cms/news/%Y/%m/",
        validators=[image_extension_validator, validate_image_file_size],
    )
    title = models.CharField(max_length=150)
    slug = models.SlugField(unique=True, help_text="URL-safe identifier — auto-filled from the title in the admin.")
    excerpt = models.CharField(max_length=250)
    body = CKEditor5Field(config_name="default", blank=True, help_text="Optional — for a future full article page. The homepage card only shows the excerpt.")
    published_date = models.DateField()
    is_published = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-published_date"]
        verbose_name = "News post"

    def __str__(self):
        return self.title


class FAQItem(models.Model):
    icon_class = models.CharField(max_length=60)
    question = models.CharField(max_length=150)
    answer = CKEditor5Field(config_name="default")
    is_published = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]
        verbose_name = "FAQ item"
        verbose_name_plural = "FAQ items"

    def __str__(self):
        return self.question


class TeamMember(models.Model):
    photo = models.ImageField(
        upload_to="cms/team/",
        validators=[image_extension_validator, validate_image_file_size],
    )
    name = models.CharField(max_length=100)
    role = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True, help_text="Unpublish without deleting the person's record.")
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]
        verbose_name = "Team member"

    def __str__(self):
        return f"{self.name} — {self.role}"


class ContactMessage(models.Model):
    """Fills in the contact form found to go nowhere during the review of
    app/ — see design doc §1 and §6."""

    name = models.CharField(max_length=100)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True)
    subject = models.CharField(max_length=100)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Contact message"
        verbose_name_plural = "Contact messages"

    def __str__(self):
        return f"{self.name} — {self.subject}"
