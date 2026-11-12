def build_filter(settings):
    # This trusted local extension returns a SQL expression and bound values.
    return "length(text) >= :min_length", {"min_length": 5}
