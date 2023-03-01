from .exceptions_types import EmailUndeliverableError

import dns.resolver
import dns.exception


def caching_resolver(*, timeout=None, cache=None):
    if timeout is None:
        from . import DEFAULT_TIMEOUT
        timeout = DEFAULT_TIMEOUT
    resolver = dns.resolver.Resolver()
    resolver.cache = cache or dns.resolver.LRUCache()
    resolver.lifetime = timeout  # timeout, in seconds
    return resolver


def validate_email_deliverability(domain, domain_i18n, timeout=None, dns_resolver=None):
    # Check that the domain resolves to an MX record. If there is no MX record,
    # try an A or AAAA record which is a deprecated fallback for deliverability.
    # Raises an EmailUndeliverableError on failure. On success, returns a dict
    # with deliverability information.

    # If no dns.resolver.Resolver was given, get dnspython's default resolver.
    # Override the default resolver's timeout. This may affect other uses of
    # dnspython in this process.
    if dns_resolver is None:
        from . import DEFAULT_TIMEOUT
        if timeout is None:
            timeout = DEFAULT_TIMEOUT
        dns_resolver = dns.resolver.get_default_resolver()
        dns_resolver.lifetime = timeout

    deliverability_info = {}

    try:
        try:
            # Try resolving for MX records (RFC 5321 Section 5).
            response = dns_resolver.resolve(domain, "MX")

            # For reporting, put them in priority order and remove the trailing dot in the qnames.
            mtas = sorted([(r.preference, str(r.exchange).rstrip('.')) for r in response])

            # RFC 7505: Null MX (0, ".") records signify the domain does not accept email.
            # Remove null MX records from the mtas list (but we've stripped trailing dots,
            # so the 'exchange' is just "") so we can check if there are no non-null MX
            # records remaining.
            mtas = [(preference, exchange) for preference, exchange in mtas
                    if exchange != ""]
            if len(mtas) == 0:  # null MX only, if there were no MX records originally a NoAnswer exception would have occurred
                raise EmailUndeliverableError("The domain name %s does not accept email." % domain_i18n)

            deliverability_info["mx"] = mtas
            deliverability_info["mx_fallback_type"] = None

        except dns.resolver.NoAnswer:
            # If there was no MX record, fall back to an A record. (RFC 5321 Section 5)
            try:
                response = dns_resolver.resolve(domain, "A")
                deliverability_info["mx"] = [(0, str(r)) for r in response]
                deliverability_info["mx_fallback_type"] = "A"

            except dns.resolver.NoAnswer:

                # If there was no A record, fall back to an AAAA record.
                # (It's unclear if SMTP servers actually do this.)
                try:
                    response = dns_resolver.resolve(domain, "AAAA")
                    deliverability_info["mx"] = [(0, str(r)) for r in response]
                    deliverability_info["mx_fallback_type"] = "AAAA"

                except dns.resolver.NoAnswer:
                    # If there was no MX, A, or AAAA record, then mail to
                    # this domain is not deliverable, although the domain
                    # name has other records (otherwise NXDOMAIN would
                    # have been raised).
                    raise EmailUndeliverableError("The domain name %s does not accept email." % domain_i18n)

            # Check for a SPF (RFC 7208) reject-all record ("v=spf1 -all") which indicates
            # no emails are sent from this domain (similar to a Null MX record
            # but for sending rather than receiving). In combination with the
            # absence of an MX record, this is probably a good sign that the
            # domain is not used for email.
            try:
                response = dns_resolver.resolve(domain, "TXT")
                for rec in response:
                    value = b"".join(rec.strings)
                    if value.startswith(b"v=spf1 "):
                        deliverability_info["spf"] = value.decode("ascii", errors='replace')
                        if value == b"v=spf1 -all":
                            raise EmailUndeliverableError("The domain name %s does not send email." % domain_i18n)
            except dns.resolver.NoAnswer:
                # No TXT records means there is no SPF policy, so we cannot take any action.
                pass

    except dns.resolver.NXDOMAIN:
        # The domain name does not exist --- there are no records of any sort
        # for the domain name.
        raise EmailUndeliverableError("The domain name %s does not exist." % domain_i18n)

    except dns.resolver.NoNameservers:
        # All nameservers failed to answer the query. This might be a problem
        # with local nameservers, maybe? We'll allow the domain to go through.
        return {
            "unknown-deliverability": "no_nameservers",
        }

    except dns.exception.Timeout:
        # A timeout could occur for various reasons, so don't treat it as a failure.
        return {
            "unknown-deliverability": "timeout",
        }

    except EmailUndeliverableError:
        # Don't let these get clobbered by the wider except block below.
        raise

    except Exception as e:
        # Unhandled conditions should not propagate.
        raise EmailUndeliverableError(
            "There was an error while checking if the domain name in the email address is deliverable: " + str(e)
        )

    return deliverability_info
