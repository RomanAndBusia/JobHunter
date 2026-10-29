"""Application intent, independent of the suitability model's recommendation."""


def explicit_direct_apply(application):
    origin = (application.review_data or {}).get('origin')
    # A campaign can reuse a source-neutral job, but never inherits user intent.
    if origin == 'campaign':
        return False
    return origin in {'direct_url', 'apply_by_url', 'user_selected'} or application.job.source == 'direct'
