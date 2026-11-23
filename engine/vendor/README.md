# Vendored files for offline decks

Decks must render with no network: in a lecture hall, on a plane, or when a CDN
is down. The engine therefore ships its own copy of KaTeX and of the theme
fonts. Nothing here is edited by hand; replace a file only by downloading the
same version again and updating its checksum below.

Downloaded 28 October 2026.

## KaTeX 0.16.21 (`engine/vendor/katex/`)

- Source: the npm package `katex@0.16.21`,
  `https://registry.npmjs.org/katex/-/katex-0.16.21.tgz`
  (tarball sha256 `7764a23b6c42d0a260fc1c8dcfe91d73e07d802dfc0624c08997b33c02dac0cd`,
  npm sha1 `8f63c659e931b210139691f2cc7bb35166b792a3`). The files are the
  package's `dist/` files, byte-identical to
  `https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/...`, which is what decks link.
- Kept: `katex.min.css`, `katex.min.js`, `contrib/auto-render.min.js` and all
  60 files of `fonts/` (woff2, woff and ttf of every face, so the CSS's own
  relative `fonts/` URLs resolve).
- Licence: MIT (`engine/vendor/katex/LICENSE`, copied from the package). The
  KaTeX fonts are distributed under the same licence in the package.
- Used by: the engine's KaTeX fallback in `engine/webslides.js`, which loads
  this copy only when the deck's CDN KaTeX script or stylesheet fails to load.

## Fonts (`engine/fonts/`)

- Lato 400, 400 italic, 700, 700 italic (Google Fonts `lato/v25`) and
  JetBrains Mono 400 (Google Fonts `jetbrainsmono/v24`).
- Source: the static woff2 files that
  `https://fonts.googleapis.com/css2?family=Lato:ital,wght@0,400;0,700;1,400;1,700&family=JetBrains+Mono:wght@400&display=swap`
  lists for a Chrome user agent, one file per Google subset, with the same
  `unicode-range` in `engine/themes/academic.css`. The upright Lato and
  JetBrains Mono files are exactly the ones the theme's old Google Fonts
  `@import` loaded, so upright text renders identically.
- Licences: SIL Open Font License 1.1 (`engine/fonts/OFL-Lato.txt`,
  `engine/fonts/OFL-JetBrainsMono.txt`, from `github.com/google/fonts`
  `ofl/lato/OFL.txt` and `ofl/jetbrainsmono/OFL.txt`).

| File | Source URL |
|------|------------|
| lato-400-normal-latin.woff2 | https://fonts.gstatic.com/s/lato/v25/S6uyw4BMUTPHjx4wXiWtFCc.woff2 |
| lato-400-normal-latin-ext.woff2 | https://fonts.gstatic.com/s/lato/v25/S6uyw4BMUTPHjxAwXiWtFCfQ7A.woff2 |
| lato-400-italic-latin.woff2 | https://fonts.gstatic.com/s/lato/v25/S6u8w4BMUTPHjxsAXC-qNiXg7Q.woff2 |
| lato-400-italic-latin-ext.woff2 | https://fonts.gstatic.com/s/lato/v25/S6u8w4BMUTPHjxsAUi-qNiXg7eU0.woff2 |
| lato-700-normal-latin.woff2 | https://fonts.gstatic.com/s/lato/v25/S6u9w4BMUTPHh6UVSwiPGQ3q5d0.woff2 |
| lato-700-normal-latin-ext.woff2 | https://fonts.gstatic.com/s/lato/v25/S6u9w4BMUTPHh6UVSwaPGQ3q5d0N7w.woff2 |
| lato-700-italic-latin.woff2 | https://fonts.gstatic.com/s/lato/v25/S6u_w4BMUTPHjxsI5wq_Gwftx9897g.woff2 |
| lato-700-italic-latin-ext.woff2 | https://fonts.gstatic.com/s/lato/v25/S6u_w4BMUTPHjxsI5wq_FQftx9897sxZ.woff2 |
| jetbrains-mono-400-normal-latin.woff2 | https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbY2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKxTOlOVk6OThhvA.woff2 |
| jetbrains-mono-400-normal-latin-ext.woff2 | https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbY2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKxTNFOVk6OThhvAWV8.woff2 |
| jetbrains-mono-400-normal-cyrillic.woff2 | https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbY2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKxTPlOVk6OThhvAWV8.woff2 |
| jetbrains-mono-400-normal-cyrillic-ext.woff2 | https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbY2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKxTN1OVk6OThhvAWV8.woff2 |
| jetbrains-mono-400-normal-greek.woff2 | https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbY2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKxTOVOVk6OThhvAWV8.woff2 |
| jetbrains-mono-400-normal-vietnamese.woff2 | https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbY2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKxTNVOVk6OThhvAWV8.woff2 |

## SHA-256 checksums (paths relative to `engine/`)

Check with `cd engine && sed -n '/^```sha256/,/^```$/p' vendor/README.md | grep -v '```' | shasum -a 256 -c`.

```sha256
f787891b550d554c214aa8902f39ac46df2dbd48fdec500a2040a5dce1e8ab58  vendor/katex/katex.min.css
863811e2baa0849c77bc92d26d44f4d0a4843c2a8ab52c462017dea57316e4d8  vendor/katex/katex.min.js
bb53eb953394531aae36fdd537065c4244eb8542901a3ce914601d932675b8ac  vendor/katex/contrib/auto-render.min.js
766ccc1f306c885aa45542a9846bbd0a505b27a0374f146778171c2254ce18e3  vendor/katex/LICENSE
68534840bcfdd2bffb6f0e8deb48684dd01e7f04ea2813267577afb906de1d13  vendor/katex/fonts/KaTeX_AMS-Regular.ttf
30da91e84c893f875e252689faebdc590b2871145e8adc7f9a9d4dbd8ce0b251  vendor/katex/fonts/KaTeX_AMS-Regular.woff
0cdd387c9590a1a9f9794560022dbb59654a7d86f187aa0c81495ad42d3a7308  vendor/katex/fonts/KaTeX_AMS-Regular.woff2
07d8e303ce4fc12b4bb54f1004170dd190a1f3db45d400fe68060df3e0897268  vendor/katex/fonts/KaTeX_Caligraphic-Bold.ttf
1ae6bd7475590e97e7f145a89e09ccde322f7a6bc0b91607b1c8b8ee28290fed  vendor/katex/fonts/KaTeX_Caligraphic-Bold.woff
de7701e42cf1f4cf0b766c03fb27977207eee2f4fd5d76fa82188406da43ea4c  vendor/katex/fonts/KaTeX_Caligraphic-Bold.woff2
ed0b74372feefcbb9c0666b2e210da37b7e49fa7fbbf3eeb11db5f693dacfbb7  vendor/katex/fonts/KaTeX_Caligraphic-Regular.ttf
3398dd02302557a793f2863f88e02d96ce10df2abffa07c8e9fa90775116e65c  vendor/katex/fonts/KaTeX_Caligraphic-Regular.woff
5d53e70ad607c2352162dec9e0923fb54ecdafaccbf604cd8dcf7d00facb989b  vendor/katex/fonts/KaTeX_Caligraphic-Regular.woff2
9163df9c7122432e6495b4229fa9071cf9ae86a758ae5efc4924ec2e1a6dbce1  vendor/katex/fonts/KaTeX_Fraktur-Bold.ttf
9be7ceb88004ab8ad124082246fbfcca4091e36385d4ec6ed1df67375dad50fb  vendor/katex/fonts/KaTeX_Fraktur-Bold.woff
74444efd593c005e3f4573b44524704c0af0a937fe911cca9e94068d0d140d3f  vendor/katex/fonts/KaTeX_Fraktur-Bold.woff2
1e6f9579e90e2cac37f8f60a597c436e075c114385652b7cbeb0dec0421291b3  vendor/katex/fonts/KaTeX_Fraktur-Regular.ttf
5e28753be717dac97f559f49bc10be9cf3c124ddcabda6659d11cb68febc6463  vendor/katex/fonts/KaTeX_Fraktur-Regular.woff
51814d270d06ff0255dba0799994fa4d8c84d11f09951d47595f4abb1f3602dc  vendor/katex/fonts/KaTeX_Fraktur-Regular.woff2
138ac28d1663b3037e9c5f52371fa5c63d8324f4a38d22cd573e6ea3a3fd0cf8  vendor/katex/fonts/KaTeX_Main-Bold.ttf
c76c5d696297d51b9cb1639c7da4334f0e7dec81b42b11213b5e25ef671bb822  vendor/katex/fonts/KaTeX_Main-Bold.woff
0f60d1b897938ec918c8ce073092411baf9438f6739465693ff18b0f9d20b021  vendor/katex/fonts/KaTeX_Main-Bold.woff2
70ee1f64a20f2048c21940ef46d0144fd215baa953ca69afd1e31e98544f708f  vendor/katex/fonts/KaTeX_Main-BoldItalic.ttf
a6f7ec0d846ac7ad975adb8959c37ed49b94acbc4ae436db9ce9e20287e4a64c  vendor/katex/fonts/KaTeX_Main-BoldItalic.woff
99cd42a3c072d918f2f44984a807cf7aa16e13545fd0875fc07c6c65f99e715b  vendor/katex/fonts/KaTeX_Main-BoldItalic.woff2
0d85ae7cc30f23790a7f1a58c4a112fdca8aae769b6ba11429af1d98b1b6cb3a  vendor/katex/fonts/KaTeX_Main-Italic.ttf
f1d6ef86f3b11a528bd5185199bd2443ecb2b0dead96d88674b5a2c12be24bdf  vendor/katex/fonts/KaTeX_Main-Italic.woff
97479ca6cce906abc961ecac96faa5f9ca2e61b8e7670d475826bcdee9a7c267  vendor/katex/fonts/KaTeX_Main-Italic.woff2
d0332f52868370fd83ae7fa46470f90c8f2eab2fcf12bc4f88080b340c95a830  vendor/katex/fonts/KaTeX_Main-Regular.ttf
c6368d87e8a1a3a5d337623d83d8dc4b868f242a9ad476237d6f8d1e0f168cdc  vendor/katex/fonts/KaTeX_Main-Regular.woff
c2342cd8b869e01752a9321dc17213fc40d4d04c79688c1d43f2cf316abd7866  vendor/katex/fonts/KaTeX_Main-Regular.woff2
f9377ab0271cda59af24bcffbd46a4d0c8a3572ffafdbb38de2ad5ea7b0d5ee5  vendor/katex/fonts/KaTeX_Math-BoldItalic.ttf
850c0af5c2238497febaf5e461d880bf458c341f42f4f330f1b1ab5698b1998e  vendor/katex/fonts/KaTeX_Math-BoldItalic.woff
dc47344dbb6cb5b655c8460d561f4df5f501b90c804ad3c6cec65fe322351ab1  vendor/katex/fonts/KaTeX_Math-BoldItalic.woff2
08ce98e51b04d58945a301e639e02b6998af29fdfd61a7b8afdd07bbfc479d4a  vendor/katex/fonts/KaTeX_Math-Italic.ttf
8a8d244581371912b8f3f5a23e2437cb2a59cd9bcaebb0346e722c05737a2571  vendor/katex/fonts/KaTeX_Math-Italic.woff
7af58c5ec8f132a2ddde9027c6d7814decce4d3b822a11192a42a20e2e973264  vendor/katex/fonts/KaTeX_Math-Italic.woff2
1ece03f79f95277d57dc7f6b435a74e1379b0d46104a8530286b60ff49369ea0  vendor/katex/fonts/KaTeX_SansSerif-Bold.ttf
ece03cfd83e22c212cdef66feb8442d25a083beb988db3f1883f3f9738d750ba  vendor/katex/fonts/KaTeX_SansSerif-Bold.woff
e99ae51144bf1232efcc1bfe5add36262c6866b0faab24fa75740e1b98577a62  vendor/katex/fonts/KaTeX_SansSerif-Bold.woff2
3931dd81faed86ba021bb2bbdc36f5bed9a38d6b4f4077aca59b265aa1b02083  vendor/katex/fonts/KaTeX_SansSerif-Italic.ttf
91ee67500cc0129aa0ace3ac5c61ff1692102f0f31d02b69347fba35dcb75bf2  vendor/katex/fonts/KaTeX_SansSerif-Italic.woff
00b26ac825e2095056396e0553b8ac26d3f8ad158c3826e28b4c45b385c4714a  vendor/katex/fonts/KaTeX_SansSerif-Italic.woff2
f36ea897e19f4a2e571d1e900e4e3710e438deb05a842486045ba0a3e616a4ad  vendor/katex/fonts/KaTeX_SansSerif-Regular.ttf
11e4dc8a6471ff6d6ee561d53d10fde8f7489e798257ff449c5d37c197435605  vendor/katex/fonts/KaTeX_SansSerif-Regular.woff
68e8c73ef42afd3ccec58bf0fba302cce448938e7fc020a5e31f8a952eee1342  vendor/katex/fonts/KaTeX_SansSerif-Regular.woff2
1c67f068fea8bb09bf099c088b1cf64bd27516a6e07f4684344873564bb66a67  vendor/katex/fonts/KaTeX_Script-Regular.ttf
d96cdf2b3bdd4d64a8fd5f74a4c467f123a8a73931cd435889f08ffaf9bf947a  vendor/katex/fonts/KaTeX_Script-Regular.woff
036d4e95149b69ff9bcc0cd55771efeb25ffa3947293e69acd78d5ac328c684b  vendor/katex/fonts/KaTeX_Script-Regular.woff2
95b6d2f1a50173bfedb8c63e1d1c99b10427d0a4df4201cb44513b226951a22b  vendor/katex/fonts/KaTeX_Size1-Regular.ttf
c943cc986384f59e86bea5fd7dc50a9c4dfe567a7c05eb40d6790720dead97c9  vendor/katex/fonts/KaTeX_Size1-Regular.woff
6b47c40166b6dbe21a5dfca7718413f2147fd2399be1ba605d8ad39cedf25dfe  vendor/katex/fonts/KaTeX_Size1-Regular.woff2
a6b2099fb555c60e3a0db3a08842ebf1d732c6eb4e4bf44913613bed4fc4e39b  vendor/katex/fonts/KaTeX_Size2-Regular.ttf
2014c523c3210bcc166648c4d4cc57f05b747df07a24277bf71c51e67dc79e3d  vendor/katex/fonts/KaTeX_Size2-Regular.woff
d04c54219f9eaec6d4d4fd42dfb28785975a4794d6b2fc71e566b9cd6db842dd  vendor/katex/fonts/KaTeX_Size2-Regular.woff2
500e04d54f0d51666332c9d2089aa803be22aa878eca539e59fa53c6e522b082  vendor/katex/fonts/KaTeX_Size3-Regular.ttf
6ab6b62e9b62dae2c00dd90f791bd10950be0ecc3490d7d6045f51c2e8fe0949  vendor/katex/fonts/KaTeX_Size3-Regular.woff
73d591271b1604960cb10bb90fee021670af7297017e0e98480b332d11f51995  vendor/katex/fonts/KaTeX_Size3-Regular.woff2
c647367d1dd4e162468717d020e1fc0f1dc5c26ebfdffbe55261713bf88c5877  vendor/katex/fonts/KaTeX_Size4-Regular.ttf
99f9c6750b489c9462bf04900bd3f939df9b829339daaaaa99ef5495cdddea58  vendor/katex/fonts/KaTeX_Size4-Regular.woff
a4af7d414440a1c1790825cfb700cf9cf43b0f2c4b04f0ebc523011ad9853ec0  vendor/katex/fonts/KaTeX_Size4-Regular.woff2
f01f3e87d9c6a61c0c081ceb577abd864eb00a612f7ac1620dd6915fad2ef5aa  vendor/katex/fonts/KaTeX_Typewriter-Regular.ttf
e14fed02b1aba7ce9f5afd5844b5d0321b22351febc720e0de8b8723527609f7  vendor/katex/fonts/KaTeX_Typewriter-Regular.woff
71d517d67827787cfabdf186914cc3358eda539e37931941f2b2fd4a21f68c0b  vendor/katex/fonts/KaTeX_Typewriter-Regular.woff2
d4ae5188a65370ecfe28f42293bbee8297cfd5712c6aadfdb270d48f2bcd88b0  fonts/lato-400-normal-latin.woff2
b85c84c655bcbba3bd628b2c1fea0fbdb5b158a9022281a2f910e82fd6da5098  fonts/lato-400-normal-latin-ext.woff2
5c3360f8439a1a3f48e8b58fcb6f3a3cbaaa76488a1b846acfe1e3b623c0562f  fonts/lato-400-italic-latin.woff2
faa2f0453abc8d16a6a13856d79be9c426ca0e1e492e1edc735d135f2772b2e2  fonts/lato-400-italic-latin-ext.woff2
7a7ce1a34f3e9944fe88fc61abbc93b6db383afa2b90815fd7ccea456fbce4e5  fonts/lato-700-normal-latin.woff2
78e850d569834892d3f4bdfd2084c7a7c0dc64ae4226f3231af30dae706400e2  fonts/lato-700-normal-latin-ext.woff2
4b833d8ce706a334bf1349ac9e8a424d2754604bdc412ee1ea0ae75e1379ea92  fonts/lato-700-italic-latin.woff2
948d8c34cc2a93e4cd25055841f629296c83378da5352b3653e0bc4a45382c86  fonts/lato-700-italic-latin-ext.woff2
d3dcdccdaba31643defe2317942155d4af3ee03951d058adbe8bc02b13e61b0a  fonts/jetbrains-mono-400-normal-latin.woff2
d5f407e28185790518c49ec8738fa89916ddc99f793010d249165d0e2c2452cc  fonts/jetbrains-mono-400-normal-latin-ext.woff2
4188276a58108d04eb0c6f5b8c9e4b5ac2d9e31e81b2b4fa7da263b92f1b35c3  fonts/jetbrains-mono-400-normal-cyrillic.woff2
d2c25018631e008514b0cc05121554b51038987a10744515553d312de5cd2047  fonts/jetbrains-mono-400-normal-cyrillic-ext.woff2
0164d0c055dced93435b1d6c2986db2fb7d068ce21950c85b776695f56238b8a  fonts/jetbrains-mono-400-normal-greek.woff2
61f80dfe8d21d3f79252bafe3805c49de2ea80391c8e707bbf4a454026ae3ee4  fonts/jetbrains-mono-400-normal-vietnamese.woff2
74ba064d03f1f1c4a952da936c3eb71866c34404916734de3cae73b34357e59e  fonts/OFL-Lato.txt
b2fe5e8987594e9ffd1d2ca52a2f5d73eb8335243893c5d6254b5ad69269591d  fonts/OFL-JetBrainsMono.txt
```
