"""Account dashboard from the same endpoint used by MyApi's web UI."""
from .client import MyApiClient, MyApiError

RANGES = ('24h', '7d', '30d')


def fetch(period='7d'):
    if period not in RANGES:
        raise ValueError('Expected 24h, 7d or 30d')
    response = MyApiClient().request('GET', '/dashboard/device-activity', query={'range': period})
    data = response.get('data', response)
    if not isinstance(data, dict) or not all(k in data for k in ('grand', 'devices', 'allDaily', 'serviceTotals')):
        raise MyApiError('MyApi returned an unsupported dashboard response')
    # No local-call fallback: these totals must represent the connected account.
    return {k: data.get(k) for k in (
        'range', 'grand', 'deviceCount', 'activeCount', 'errorRate', 'rateLimited',
        'bucketLabels', 'allDaily', 'serviceTotals', 'devices', 'timestamp')}
