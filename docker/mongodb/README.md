# Driver MongoDB — Linux x86_64 / CPython 3.13 / musl

Le Dockerfile construit le sdist officiel PyMongo 4.19.0 en mode Python explicite
(`NO_EXT=1`). La wheel reste taguée `cp313-cp313-linux_x86_64` par le hook upstream ;
elle n’est pas renommée en wheel universelle. Le lock runtime exige le hash
réel obtenu lors de deux constructions isolées identiques :
`33798dd77ab3f174cd711b040406422a5f875f28149da335bf27c7bc1aa1dcd7`.

- `build-tools.txt` verrouille les huit outils directs et transitifs du builder.
- `source.txt` verrouille le sdist ; il est téléchargé après installation des
  outils inspectés, avec `--no-build-isolation` et sans résolution de dépendances.
- `dns.txt` verrouille la wheel ISC de dnspython.
- `runtime.txt` ne permet que la wheel locale exacte et dnspython. Les outils du
  builder ne sont pas transférés dans cet étage.
- Le build du driver est effectué avec réseau désactivé, horodatage fixe
  `SOURCE_DATE_EPOCH=1760000000`, `PYTHONHASHSEED=0` et `TZ=UTC`.

Le sdist est décrit par l’[API PyPI officielle de cette version](https://pypi.org/pypi/pymongo/4.19.0/json).
SHA-256 : `3c510dd3c5d9b392d3b33bb5d2a594758acfe8f026fca654253f947ce0af9d40`.
Les [hooks upstream](https://github.com/mongodb/mongo-python-driver/tree/4.19.0)
ont été inspectés avant leur première exécution. Une évolution de source,
d’outils ou de cible impose de reconstruire et qualifier un nouveau lock ; ne
pas contourner un échec de hash.

Les fichiers `notices/pymongo-LICENSE` et `notices/pymongo-THIRD-PARTY-NOTICES`
proviennent sans modification de ce sdist. Ils couvrent Apache-2.0 et les
composants tiers, dont time64 et son attribution. `notices/dnspython-LICENSE`
provient de la wheel officielle dnspython 2.9.2, SHA-256
`af15f7d385a6ae2fb35a796fde2138004c8c42c7389090e2addc83e3b3340fd8`.
Les trois notices sont distribuées dans l’image finale à
`/usr/share/licenses/cinevault-mongodb`. Les outils de build ne sont pas des
dépendances de l’application livrée.

Le gate d’image vérifie les versions, le mode sans extensions C, un entier BSON
au-delà de 2^53, la présence des schémas et des notices et l’absence des outils
du builder. Le gate Mongo séparé éprouve un vrai replica set. Ces vérifications
ne constituent pas encore un benchmark de charge ni une activation REST Mongo.
