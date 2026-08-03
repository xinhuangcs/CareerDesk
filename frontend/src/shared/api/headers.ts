// Fixed same-origin write marker, not a secret; cross-origin pages need CORS to send it.
export const WRITE_HEADERS = Object.freeze({ "X-CareerDesk-Request": "1" });
