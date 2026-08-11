a. Last-name privacy: members can choose to show only the initial of their last name
   across the site, so a member's full name must never be presented without honoring that
   choice. The canonical helper is SocialUser#display_name (aliased #name), which respects
   the member's hide_last_name preference. Note #full_name and #first_name_last_initial do
   NOT check that preference, so rendering a member's name to others through them (or by
   concatenating first and last name directly) bypasses the privacy logic. Flag any such
   code in views, serializers, mailers, exports, or JSON. API responses are in scope too:
   any endpoint returning a member's name must go through #display_name.
b. Investment-address privacy: when a member adds an investment address for a property
   they own, worked on, or showed interest in, that address must never be shown to other
   members. Precise location data such as latitude/longitude and the street address must
   be anonymized first. The canonical utilities are
   Api::V3::Dashboard::Map::LocationObfuscator.obfuscate (coordinates) and
   Api::V3::Dashboard::Map::StreetAddressMasker.mask (street address), designed to be used
   together. Flag any code that exposes such an address or its coordinates to other
   members or in API responses without anonymizing it.
