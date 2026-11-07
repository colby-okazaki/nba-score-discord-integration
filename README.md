# nba-score-tracker
 Discord bot that uses the ESPN API to post NBA final scores, a nightly recap, and a daily schedule image with team logos.

It runs entirely on GitHub Actions (`.github/workflows/nba-poll.yml`). Each run checks every game about 2h15m after tip-off (the average game length), then every 5 minutes until it is final. Set the `DISCORD_TOKEN` and `CHANNEL_ID` repository secrets.
