"""The site owner's essential Grateful Dead shows.

Personal Favorites renders this whole list. Dates are catalog dates so the
page can attach a real venue and an archive.org link.

Two entries in the old frontend list named a venue on a date the band did
not play there:
- 1974-06-23 is Miami, not Providence. Providence Civic Center is 1974-06-26.
- There is no Barton Hall show on 1981-05-28. Cornell 1981 is 1981-05-16.
1972-03-24 is kept as written: the Academy of Music run skipped that night,
and the favorites payload says so instead of silently swapping the date.
"""

OWNER_PICKS = [
    {"date": "1972-08-27", "venue": "Oregon Country Fair, Veneta, OR"},
    {"date": "1977-05-08", "venue": "Barton Hall, Cornell University, Ithaca, NY"},
    {"date": "1977-06-07", "venue": "Winterland Arena, San Francisco, CA"},
    {"date": "1973-12-18", "venue": "Curtis Hixon Hall, Tampa, FL"},
    {"date": "1973-06-10", "venue": "RFK Stadium, Washington, DC"},
    {"date": "1980-05-16", "venue": "Nassau Coliseum, Uniondale, NY"},
    {"date": "1971-04-26", "venue": "Fillmore East, New York, NY"},
    {"date": "1972-03-24", "venue": "Academy of Music, New York, NY"},
    {"date": "1974-06-26", "venue": "Providence Civic Center, Providence, RI"},
    {"date": "1981-03-09", "venue": "Madison Square Garden, New York, NY"},
    {"date": "1970-06-24", "venue": "Capitol Theatre, Port Chester, NY"},
    {"date": "1970-05-02", "venue": "Harpur College, Binghamton, NY"},
    {"date": "1978-07-08", "venue": "Red Rocks Amphitheatre, Morrison, CO"},
    {"date": "1972-05-25", "venue": "Lyceum Theatre, London, UK"},
    {"date": "1972-05-26", "venue": "Lyceum Theatre, London, UK"},
    {"date": "1989-04-02", "venue": "Pittsburgh Civic Arena, Pittsburgh, PA"},
    {"date": "1989-07-07", "venue": "JFK Stadium, Philadelphia, PA"},
    {"date": "1972-09-21", "venue": "The Spectrum, Philadelphia, PA"},
    {"date": "1981-05-16", "venue": "Cornell University, Ithaca, NY"},
    {"date": "1978-12-31", "venue": "Winterland Arena, San Francisco, CA"},
    {"date": "1973-10-19", "venue": "Myriad Convention Center, Oklahoma City, OK"},
    {"date": "1981-05-06", "venue": "Nassau Coliseum, Uniondale, NY"},
    {"date": "1977-09-03", "venue": "Raceway Park, Englishtown, NJ"},
    {"date": "1972-04-17", "venue": "Tivoli Concert Hall, Copenhagen, Denmark"},
]
