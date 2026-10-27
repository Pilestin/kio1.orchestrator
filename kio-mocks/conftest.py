"""Keep the sample project out of everyone else's test run.

`sample/` fails one test on purpose: it is what the stand-ins find. Run from
inside `sample/`, pytest never reads this file and the tests run as a KIO
runs them; collected from above, they would turn every run red.
"""

collect_ignore = ["sample"]
