# Provider data

| File | What it is |
|---|---|
| `providers_tz_public.csv` | Real public hospitals in the pilot districts (Kibaha, Bagamoyo, Mvomero, Morogoro town, Ilala, Temeke, Kinondoni), from the public sources in the `source` column. |
| `providers_template.csv` | Empty template for AGKI's partner clinics. |

Load either with:

```bash
python manage.py afya_import_providers afya/data/providers_tz_public.csv --dry-run   # check first
python manage.py afya_import_providers afya/data/providers_tz_public.csv
```

## Nothing imported is shown to women until staff verify it

Every new row is saved as **Pending verification**. Women are only ever
matched to **Verified** providers. Before verifying a provider in the admin:

1. Fill in `phone` (it gets the booking SMS) and `licence_number`. For
   public facilities, use the Health Facility Registry (HFR) code from the
   Ministry of Health register.
2. Check each service and its price. A service written as `need:?` (price
   unknown) is imported **inactive**. Set the real price, then tick *Is active*.
3. Only keep `handles_emergencies = yes` once the facility has confirmed it
   can take obstetric emergencies (caesarean section, blood transfusion).
4. Use the **Mark verified** action, which records who verified it and when.

## Prices at public facilities

The government states that health services for pregnant women and children
under five are free under the 2007 health policy (Deputy Health Minister,
Parliament, 3 February 2026 — Daily News). That is why antenatal and
postnatal care are `0` for public facilities here. Other prices are not
published, so they are `?`.

## Columns

- `kind`: `clinic`, `public`, `lab`, `individual`
- `council`: `mct`, `tnmc`, `pc`, `hlpc`, `moh`, `other`
- `region` / `district`: must be a place Afya Mfukoni knows (`afya/ai/language.py`, `PLACES`)
- `services`: `need:price` pairs separated by `;`. Needs: `antenatal`,
  `pregnancy_test`, `family_planning`, `sti`, `hiv`, `menstrual`, `postnatal`,
  `cervical`, `lab_test`, `mental_health`, `general`. Price in TSh, `0` =
  free, `?` = unknown (imported inactive).
- `is_youth_friendly`, `handles_emergencies`: `yes` / `no`

Re-importing the same file updates rows (matched on name + district). It
never changes a provider's verification status, and never changes a service
that already exists, so prices staff have confirmed are not overwritten.
