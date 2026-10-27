"""The one error a stand-in raises on purpose."""


class KioError(Exception):
    """The task cannot be done as asked; the message says why, for a person.

    It becomes the step's `error` in a 200 reply. Anything else a handler
    raises is a bug in the stand-in, reported the same way but logged.
    """
