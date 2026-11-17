# Good Feed

A hand-picked, Christian-first feed for Inoreader: leaders' Shorts, good news, missions, business, politics, entertainment, podcast teasers, memory cards and *The True Vine*.

**Feed address:** `https://sdarroh.github.io/good-feed/feed.xml`

## How it works

Every hour GitHub runs `build_feed.py`. At each batch time (6 AM, 9 AM, noon, 3, 6 and 9 PM Central) it:

1. Checks every source in `sources.json` and adds new posts from the last 2 weeks to a pool.
2. Picks about 10 posts from the pool, newer ones more likely, following the caps.
3. Mixes in memory cards and, in the morning, the day's *True Vine* meditation.
4. Writes `feed.xml`, which Inoreader reads.

**Caps** (counted over about the last day, about 60 posts): each person 7, leaders 22 in all, each other category 7, memory cards 5, podcast teasers 3 (the same 12.5% / 37.5% shares). Nothing repeats.

If a source stops working for 3 days, a "Feed check" card shows up in the feed.

**Reaching farther:** a source with `"lookback_days"` in `sources.json` (like Free Burma Rangers, 60 days) stays eligible longer than the usual 2 weeks, and so does any post that mentions a watch word from `config.json`; watch-word posts are also about 3× more likely to be picked. They flow in under the normal caps. A source marked `"must_see": true` instead goes to the top with a star (up to 3 a day).

## Making changes

The easy way: ask Claude ("add this pastor", "less politics", "add Psalm 121").

By hand:
- `sources.json` — who and what to pull from
- `config.json` — caps, batch times, posts per day
- `memory.txt` — memory passages (`== Psalm 121`, then one numbered verse per line)

To post a batch right away: **Actions → Update feed → Run workflow**.

## Files written automatically

`feed.xml` (the feed), `state.json` (pool and history), `status.json` (which sources are working), `truevine/` (cached meditations).

## Sharing

Anyone can subscribe to the feed address above. To give someone their own copy, they can use this repository as a template on GitHub and change `sources.json` and `memory.txt`.
