# eBay facts the mock is built on

Checked on 6 November 2026; the shipping, production and policy rows added on 8 November 2026. "Spec" means eBay's published OpenAPI files: `inventory_api.json` v1.18.8, `taxonomy_api.json` v1.1.1, `account_api_v1.json` v1.9.3 and `fulfillment_api.json` v1.20.7, all from developer.ebay.com/develop/api/spec/.

## Confirmed from the specs

| Fact | Where it matters |
| --- | --- |
| Inventory error ids: 25001 system error (500), 25002 "A user error has occurred. {additionalInfo}", 25003 price, 25004 quantity, 25005 category, 25007 / 25008 / 25009 fulfillment / payment / return policy, 25012 location, 25014 pictures, 25017 "Shipping policy is required." and similar, 25021 condition, 25022 attribute, 25025 concurrent access (500), 25702 SKU not found, 25707 invalid SKU, 25709 invalid value, 25710 not found, 25713 offer not available, 25803 "{fieldName} already exists." | `mock_ebay.py`, every publish error test |
| Error object: errorId, domain, subdomain, category (REQUEST / APPLICATION / BUSINESS), message, longMessage, inputRefIds, outputRefIds, parameters [{name, value}] | `http.py` parses it |
| Inventory item: `Content-Language` header required; SKU max 50; `product.title` max 80; `product.description` max 4000; aspects as `{"Brand": ["GoPro"]}`, aspect name max 40, value max 50; imageUrls must be https, at least one to publish, up to 24 | `validate.py` |
| Condition enum, including NEW, LIKE_NEW, NEW_OTHER, NEW_WITH_DEFECTS, refurbished grades, USED_EXCELLENT, USED_VERY_GOOD, USED_GOOD, USED_ACCEPTABLE, FOR_PARTS_OR_NOT_WORKING | `validate.CONDITIONS` |
| Only one offer per SKU + marketplace + format; updateOffer replaces the whole offer; publishOffer returns `{listingId, warnings}`; getOffers takes `sku` and `marketplace_id` | `lister.send` |
| listingDescription max 500,000 | `validate.py` |
| Business policies: opting in to SELLING_POLICY_MANAGEMENT is required to use the Inventory API and "can take up to 24-hours"; return policies need `returnsAccepted` and category type ALL_EXCLUDING_MOTORS_VEHICLES | `setup_sandbox` |
| Taxonomy: `get_default_category_tree_id`, `get_category_suggestions` (204 when nothing matches), `get_item_aspects_for_category` with aspectConstraint fields aspectRequired, aspectMode (FREE_TEXT / SELECTION_ONLY), itemToAspectCardinality (SINGLE / MULTI), aspectUsage, aspectMaxLength; error 62009 for a non-leaf category | `api.Taxonomy`, `llm.aspect_brief` |
| **getCategorySuggestions "is not supported in the Sandbox environment"** and can return boilerplate names there | `Lister.choose_category` only trusts a suggestion that shares a word with the item or agrees with the keyword map |
| Fulfillment API is served from `apiz.ebay.com` (sandbox `apiz.sandbox.ebay.com`), not `api.ebay.com`; getOrders `limit` max 200, default 50; filter `orderfulfillmentstatus:{NOT_STARTED|IN_PROGRESS}`; paged with `next` | `api.Fulfillment` |
| Scopes: api_scope, sell.inventory, sell.account, sell.fulfillment | `auth.py` |
| 429 is errorId 2002 "The request limit has been reached for the resource." | backoff tests |
| createShippingFulfillment: `POST /order/{orderId}/shipping_fulfillment`, body `lineItems` [{lineItemId, quantity}], `shippedDate` (UTC ISO 8601, defaults to now), `shippingCarrierCode`, `trackingNumber`; 201 with the new fulfillment's URI in the `Location` header (fulfillment_api.json v1.20.7, read 8 Nov) | `api.Fulfillment.create_shipping_fulfillment` |
| Carrier and tracking are "mutually dependent. If you include one, you must also include the other"; "Only alphanumeric characters are supported for shipment tracking numbers. Spaces, hyphens, and all other special characters are not supported"; with tracking, the fulfillmentId is the tracking number, without it "an arbitrary number such as 999" | `lister.clean_tracking`, mock |
| createShippingFulfillment errors: 32100 invalid order id, 32200 invalid line item id, 32210 duplicate line item, 32300 invalid tracking number or carrier, 32400 user suspended, 32500 invalid shipped date, 32600 invalid input data, 34100 too many tracking numbers, 34500 "use PUT for updating", 30500 system error; 409 Conflict is listed with no error ids | mock, shipping tests |
| getShippingFulfillments returns `{fulfillments, total, warnings}`; a fulfillment has fulfillmentId, lineItems, shipmentTrackingNumber, shippedDate | idempotent re-runs of `ship` |
| Production hosts: `https://api.ebay.com` and `https://apiz.ebay.com` (servers list), consent `https://auth.ebay.com/oauth2/authorize`, token `https://api.ebay.com/identity/v1/oauth2/token` (the spec's OAuth securitySchemes) | `auth.PRODUCTION` |

## eBay's listing policies (ebay.com help pages)

Read on the day (8 Nov), search manipulation policy (help id 4243): "Using keywords that are not directly related to the item for sale is not allowed", "Listings that make comparisons with other products are not allowed", "Listings that promote other listings are not allowed", "Listings can't include keywords with question marks" (leave an unknown detail out). Third-party endorsements policy (help id 4249): claims like "Buy with confidence from a trusted seller" are not allowed; checkable claims ("over 1,000 positive feedback ratings") are. These are the rules behind `policy.py`'s question mark, comparison, other-brand, promotion and endorsement checks.

Not readable from here on the day (eBay's pages answered with a bot check or opened another article), so taken from eBay's policies as commonly quoted: the counterfeit/replica policy (replica, knock-off, "inspired by"), the links policy and the policy on offers to buy or sell outside eBay (contact details, web addresses, payment outside eBay). The exact thresholds for shouting, filler words and repeated keywords are this tool's own.

## The mock's best guess (eBay's pages were not readable from here)

These come from eBay's documentation as commonly quoted, not from a page read on the day. The live sandbox test settles them.

- Sandbox hosts `api.sandbox.ebay.com` and `auth.sandbox.ebay.com`, token endpoint `/identity/v1/oauth2/token`, access tokens 7200 seconds, refresh tokens about 18 months, OAuth errors as `{"error", "error_description"}`.
- The exact wording when publishOffer finds a required item specific missing ("The item specific Brand is missing...", errorId 25002). The spec also lists 25029 "{field} is required for this category."
- The errorId when a second offer is created for the same SKU (the mock uses 25002 "Offer entity already exists." with the offerId as a parameter; the code only relies on the offerId parameter and falls back to raising).
- getOffers answering 404 / 25713 when a SKU has no offer yet (the client treats 404 as "no offers").
- Daily call limits.
- The error body for a second createShippingFulfillment on a line item that is already shipped (the spec lists 409 Conflict with no error ids; the mock answers 409 with errorId 0). The tool avoids it by reading the order's fulfillments and line item statuses first.
- The mock's error ids for getShippingFulfillment of an unknown fulfillment (32110) and for a bad limit or filter on getOrders (30700, 30800).
- Whether every carrier code eBay accepts matches `[A-Za-z0-9_]` (eBay points to the Trading API's GeteBayDetails for the list).
