"""Error codes and result helpers for the session lease manager.

Every operation returns a plain dict.  Success results have ``ok == True``
and ``code == "OK"``; failures have ``ok == False`` and one of the codes
below.  Failure dicts carry a human readable ``error`` message whose exact
text is NOT part of the stable contract (tests compare ``code`` only).
"""

OK = "OK"
INVALID_FIELD = "INVALID_FIELD"          # bad type / value, rejected before the business op
SESSION_EXISTS = "SESSION_EXISTS"        # session_id already used in this tenant
SESSION_NOT_FOUND = "SESSION_NOT_FOUND"  # no such session in this tenant
SESSION_EXPIRED = "SESSION_EXPIRED"      # session exists but is past a deadline
SESSION_NOT_ACTIVE = "SESSION_NOT_ACTIVE"  # logged out or reclaimed
CONFLICT = "CONFLICT"                    # request_id reused with different parameters
UNKNOWN_OP = "UNKNOWN_OP"                # unknown operation name in the input


def ok(**fields):
    result = {"ok": True, "code": OK}
    result.update(fields)
    return result


def err(code, message, **fields):
    result = {"ok": False, "code": code, "error": message}
    result.update(fields)
    return result
