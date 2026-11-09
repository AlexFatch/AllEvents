# Zimbabwe event snapshot

Reviewed on 5 November 2026, for local event discovery. These are external
listings, with bookings handled by the source provider. No local ticket stock,
prices, sales counts or organiser affiliation are created.

Sources:

- [MegaTicket](https://www.megaticket.africa/): Sip and Knit, The Mesh,
  5 Million Smiles, The Cosmos & Science Nerds Zimbabwe.
- [CIMAM](https://www.cimam.org/cimam-annual-conference/annual-conference-2026-harare/):
  Annual Conference, 20–22 November, hosted by the National Gallery of Zimbabwe.
- [TicketBox](https://ticketbox.co.zw/): Gateway anniversary concert and The Unveiled
  in Bulawayo.
- [Hype Nation](https://tickets.hypenation.co.zw/tickets): Honde Valley Marathon,
  In Search of The King (27 November evening), Unplugged (20 December).

The JSON includes the source URL for every event. Where the source did not
expose a stable detail page, the link opens its listing catalogue. Descriptions
are short original summaries. SVG category artwork is illustrative, not an
official event poster. Unpublished start times are left unspecified. The
5 Million Smiles listing gives a 4 December start but has an inconsistent end
date; its description asks visitors to confirm scheduling with the provider.

Import with `python3 scripts/import_local_events.py` from the project root.
The importer saves a SQLite backup in `backups/`, skips existing title/date
pairs, and preserves edits to previously imported records. This snapshot does
not automatically update when an organiser changes details.
