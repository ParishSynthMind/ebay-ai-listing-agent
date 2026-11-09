# eBay facts the mock is built on

Checked on 6 November 2026; the shipping, production and policy rows added on 8 November 2026; the live sandbox test on 9 November 2026 (first section below) settled or corrected the rest. "Spec" means eBay's published OpenAPI files: `inventory_api.json` v1.18.8, `taxonomy_api.json` v1.1.1, `account_api_v1.json` v1.9.3 and `fulfillment_api.json` v1.20.7, all from developer.ebay.com/develop/api/spec/.

## Checked live on the eBay sandbox (9 November 2026)

Seller `testuser_neoseller2`, EBAY_US, category tree 0 version 134. The CLI transcript is in [`examples/live-sandbox/terminal.txt`](../examples/live-sandbox/terminal.txt); what eBay said:

| What we did | What eBay answered | What changed here |
| --- | --- | --- |
| Application token, user token from the refresh token, default category tree | all fine; tree id `0` | nothing |
| Opt in to business policies, then create the three policies straight away | opt-in accepted and the policies were created in the same minute (no 24-hour wait in practice) | nothing |
| getCategorySuggestions "35mm film camera", "coffee mug", "running shoes" | **sensible answers**, not boilerplate: Film Cameras 15230, Mugs 20695, Athletic Shoes 15709; no `relevancy` field | the keyword map now agrees with eBay; mock drops `relevancy` |
| getItemAspectsForCategory 15232 | **400 errorId 62005** "The specified category ID does not belong to specified category tree." | **the mock's camera id was wrong**: Film Cameras is 15230 (under Film Photography 69323) |
| getItemAspectsForCategory 20638 | a kitchen storage category (knife blocks), requiring Brand, Item Height, Item Width | **the mock's mug id was wrong**: Mugs is 20695 (Home & Garden 11700 > Kitchen, Dining & Bar 20625 > Dinnerware & Serveware 36027) |
| Required item specifics | 15709: Brand, Color, Department (SELECTION_ONLY: Men, Teens, Unisex Adults), US Shoe Size (FREE_TEXT). 15230: Brand, Type (FREE_TEXT, e.g. 35mm, SLR), Model. 20695: Type, Brand, Color. **Color is SINGLE in all three** | mock copied from these; a second value on a one-value specific is now dropped with a note and a check line |
| Metadata getItemConditionPolicies | 15230: 1000, 1500, 2500, 3000, 7000. 15709: 1000, 1500, 1750, 2990, 3000, 3010. 20695: 1000, 1500, 3000 | **new check**: the tool reads these, tells the AI, and stops a condition the category does not take |
| publishOffer with condition USED_GOOD in 15230 | **400 errorId 25021** "...has invalid item condition information. The provided condition id is invalid for the selected primary category id." | mock enforces it |
| getOffers for a SKU with no offer | 404 errorId 25713 "This Offer is not available." | the mock's guess was right |
| createOffer a second time for the same SKU | 400 errorId 25002 "A user error has occurred. Offer entity already exists." with parameter `offerId` | the mock's guess was right |
| getInventoryItem for an unknown SKU | 404 errorId 25710 | nothing |
| A bad refresh token | 400 `invalid_grant` "the provided authorization refresh token is invalid or was issued to another client" | the mock's guess was right |
| The application token on a Sell API | 403 errorId 1100 "Access denied" | nothing |
| getOrders on `apiz.sandbox.ebay.com` | **404 errorId 2002 "Resource not found"**; on `api.sandbox.ebay.com` 200. Production answers the same way (apiz 404, api 401 without a token) | **the spec's apiz host was wrong for Fulfillment**: the tool now calls api |
| updateOffer with a new price (repricing) | accepted; getOffers reads the new price back. eBay may hand a price back as `"165.0"` | nothing |
| publishOffer, every attempt on the morning of 9 November (from the tool, from raw calls, and retried every 20 minutes) | **500 errorId 25001 "Internal Server Error"** or 400 errorId 25002 "System error" / "Internal Application Error" (parameter `err:216128`). The Trading API's VerifyAddFixedPriceItem gave the same (ErrorCode 10007 / 21916803) for every variant tried; without a picture it got as far as "Add at least 1 photo", and picture upload itself (UploadSiteHostedPictures) worked | an eBay sandbox fault, not this tool: eBay's status page lists the same fault as resolved incidents before. Publishing, and orders and shipping after it, wait for eBay |

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
| **getCategorySuggestions "is not supported in the Sandbox environment"** and can return boilerplate names there (on 9 November it answered sensibly) | `Lister.choose_category` only trusts a suggestion that shares a word with the item or agrees with the keyword map |
| ~~Fulfillment API is served from `apiz.ebay.com`~~ (the spec's servers list says so; the live sandbox and production answer 404 there, so the tool uses `api`); getOrders `limit` max 200, default 50; filter `orderfulfillmentstatus:{NOT_STARTED|IN_PROGRESS}`; paged with `next` | `api.Fulfillment` |
| Scopes: api_scope, sell.inventory, sell.account, sell.fulfillment | `auth.py` |
| 429 is errorId 2002 "The request limit has been reached for the resource." | backoff tests |
| createShippingFulfillment: `POST /order/{orderId}/shipping_fulfillment`, body `lineItems` [{lineItemId, quantity}], `shippedDate` (UTC ISO 8601, defaults to now), `shippingCarrierCode`, `trackingNumber`; 201 with the new fulfillment's URI in the `Location` header (fulfillment_api.json v1.20.7, read 8 Nov) | `api.Fulfillment.create_shipping_fulfillment` |
| Carrier and tracking are "mutually dependent. If you include one, you must also include the other"; "Only alphanumeric characters are supported for shipment tracking numbers. Spaces, hyphens, and all other special characters are not supported"; with tracking, the fulfillmentId is the tracking number, without it "an arbitrary number such as 999" | `lister.clean_tracking`, mock |
| createShippingFulfillment errors: 32100 invalid order id, 32200 invalid line item id, 32210 duplicate line item, 32300 invalid tracking number or carrier, 32400 user suspended, 32500 invalid shipped date, 32600 invalid input data, 34100 too many tracking numbers, 34500 "use PUT for updating", 30500 system error; 409 Conflict is listed with no error ids | mock, shipping tests |
| getShippingFulfillments returns `{fulfillments, total, warnings}`; a fulfillment has fulfillmentId, lineItems, shipmentTrackingNumber, shippedDate | idempotent re-runs of `ship` |
| Production hosts: `https://api.ebay.com`, consent `https://auth.ebay.com/oauth2/authorize`, token `https://api.ebay.com/identity/v1/oauth2/token` (the spec's OAuth securitySchemes) | `auth.PRODUCTION` |

## eBay's listing policies (ebay.com help pages)

Read on the day (8 Nov), search manipulation policy (help id 4243): "Using keywords that are not directly related to the item for sale is not allowed", "Listings that make comparisons with other products are not allowed", "Listings that promote other listings are not allowed", "Listings can't include keywords with question marks" (leave an unknown detail out). Third-party endorsements policy (help id 4249): claims like "Buy with confidence from a trusted seller" are not allowed; checkable claims ("over 1,000 positive feedback ratings") are. These are the rules behind `policy.py`'s question mark, comparison, other-brand, promotion and endorsement checks.

Not readable from here on the day (eBay's pages answered with a bot check or opened another article), so taken from eBay's policies as commonly quoted: the counterfeit/replica policy (replica, knock-off, "inspired by"), the links policy and the policy on offers to buy or sell outside eBay (contact details, web addresses, payment outside eBay). The exact thresholds for shouting, filler words and repeated keywords are this tool's own.

## The mock's best guess (eBay's pages were not readable from here)

These come from eBay's documentation as commonly quoted, not from a page read on the day. The sandbox hosts, token endpoint, OAuth error shape, the duplicate-offer error and getOffers' 404 / 25713 are now confirmed live (above). Still open, because eBay's sandbox would not publish on the day:

- The exact wording when publishOffer finds a required item specific missing ("The item specific Brand is missing...", errorId 25002). The spec also lists 25029 "{field} is required for this category."
- Daily call limits.
- The error body for a second createShippingFulfillment on a line item that is already shipped (the spec lists 409 Conflict with no error ids; the mock answers 409 with errorId 0). The tool avoids it by reading the order's fulfillments and line item statuses first.
- The mock's error ids for getShippingFulfillment of an unknown fulfillment (32110) and for a bad limit or filter on getOrders (30700, 30800).
- Whether every carrier code eBay accepts matches `[A-Za-z0-9_]` (eBay points to the Trading API's GeteBayDetails for the list).
