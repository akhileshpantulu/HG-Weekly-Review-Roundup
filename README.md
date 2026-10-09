# HG Weekly Review Roundup: Marriott and IHG hotels

Every Friday, a GitHub Actions workflow pulls guest reviews for a list of Marriott and IHG hotels from the public Bazaarvoice API (the same one marriott.com and ihg.com use on their review pages), compares them to last week, and opens a GitHub Issue with the roundup. **GitHub emails you the issue automatically.** Same mechanism as MPLV-Review-Alerts: no SMTP account or extra email address.

## What the email contains

For the whole portfolio, one overview table. For each hotel:

- **Total reviews** and the change since last week
- **Average star rating** and the change since last week
- **Star distribution**: new reviews this week by star (5 to 1), plus the all-time count per star and its weekly change
- **Summary of the new reviews**: written by Claude if an `ANTHROPIC_API_KEY` secret is set (praise, complaints, low-score items to follow up on); otherwise a short statistical summary
- The full list of new reviews (collapsed), with 1 and 2 star reviews flagged **LOW SCORE**

The issue title shows the total new reviews and how many were low scores.

## How it works

1. `weekly_roundup.py` reads `hotels.json`, then calls the Bazaarvoice API for each hotel: review statistics plus all reviews submitted in the last 60 days.
2. New reviews are the ones whose IDs were not seen last week. Looking back 60 days catches reviews that were written earlier but published late.
3. Last week's totals, average, star distribution and seen review IDs live in `state.json`, which the workflow commits back after each run.
4. The first run for any hotel sets its baseline; changes for that hotel start the following week.

## Setup

1. Push this repo to GitHub (keep `.github/workflows/weekly-roundup.yml` at that path).
2. Make sure GitHub notification email is on: https://github.com/settings/notifications, under "Subscriptions > Watching", tick **Email**.
3. Optional but recommended, for the written review summaries: in the repo go to **Settings > Secrets and variables > Actions > New repository secret**, name it `ANTHROPIC_API_KEY`, paste a key from https://console.anthropic.com.
4. Fill in `hotels.json` (below).
5. Go to the **Actions** tab, select "Weekly review roundup", click **Run workflow** once to set the baseline. The next Friday run sends the first real roundup.

## Adding hotels (`hotels.json`)

```json
{
  "passkeys": {
    "marriott": "canCX9lvC812oa4Y6HYf4gmWK5uszkZCKThrdtYkZqcYE",
    "ihg": "<IHG passkey>"
  },
  "hotels": [
    {
      "name": "Moxy Paris La Villette",
      "brand": "marriott",
      "product_id": "parvx",
      "reviews_url": "https://www.marriott.com/en-us/hotels/parvx-moxy-paris-la-villette/reviews/"
    },
    {
      "name": "Example IHG hotel",
      "brand": "ihg",
      "product_id": "<IHG product ID>",
      "reviews_url": "https://www.ihg.com/..."
    }
  ]
}
```

- `brand` is `marriott` or `ihg` and picks the passkey. A hotel can also carry its own `"passkey"`.
- Add `"enabled": false` to pause a hotel without deleting it.
- **Marriott**: `product_id` is the five-letter MARSHA code from the hotel's marriott.com URL (e.g. `parvx` in `/hotels/parvx-moxy-paris-la-villette/`).
- **IHG**: the IHG passkey and product ID still need to be read from ihg.com once. Open a hotel's page on ihg.com, open the browser developer tools (F12), go to the **Network** tab, filter for `bazaarvoice`, then scroll to or open the reviews section. A request to `api.bazaarvoice.com/data/...` (or `apps.bazaarvoice.com`) carries `passkey=...` and `Filter=ProductId:...` in its URL. Put the passkey under `passkeys.ihg` and the product ID in the hotel entry. Until the IHG passkey is set, IHG hotels are listed as "Skipped" in the email.

## Notes

- Schedule: Fridays at 13:07 UTC (9:07am ET in summer, 8:07am ET in winter). Change the `cron` line in the workflow to move it. GitHub can start scheduled runs a few minutes late.
- Passkeys are public client-side keys embedded in each brand's website. If one is rotated, that hotel shows under "Errors" in the email (or the run fails and GitHub emails you); read the new key the same way as above.
- If a hotel fails to load, its previous snapshot is kept so the next successful week compares against it.
- Summaries use the Claude API (one request per hotel with new reviews); with no key set, nothing is sent to Anthropic.
