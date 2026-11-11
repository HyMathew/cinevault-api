# Packaging du driver MongoDB dans l’image API

Objectif : livrer le codec BSON, PyMongo et les schémas nécessaires dans l’image
Alpine existante, sans changer le backend actif ni toucher au service stable.
Le mode JSON et ses dépendances Python restent utilisables séparément.

## Décision

PyMongo 4.19.0 ne fournit pas de wheel musllinux. Le sdist officiel déjà inspecté
est construit avec `NO_EXT=1` dans un étage CPython 3.13/Alpine x86_64. Deux
constructions isolées ont produit le SHA-256
`33798dd77ab3f174cd711b040406422a5f875f28149da335bf27c7bc1aa1dcd7`.
L’installation finale doit refuser un résultat différent. Aucun retag, wheel
glibc forcée ou compilateur dans le runtime. Les outils du builder et la source
sont verrouillés par version et hash ; la construction du driver est hors réseau.

## Travail et preuves attendues

- [x] Observer le refus d’import du SDK dans l’image de référence, sans modifier
  son service ; cette image ne constitue pas le runtime Mongo final.
- [x] Ajouter les trois locks `docker/mongodb/{build-tools,source,runtime}.txt`,
  les notices Apache/tiers/ISC et la provenance ; aucun binaire vendored.
- [x] Ajouter l’étage builder au Dockerfile et copier les schémas malgré
  l’exclusion JSON générale du contexte Docker.
- [x] Renforcer le gate d’image : import/version du driver, mode Python, grand
  entier BSON exact, schémas disponibles et absence des outils du builder.
- [x] Construire réellement le candidat et exécuter le smoke en UID10001,
  réseau désactivé, lecture seule ; conserver identité et empreintes des sources.
- [x] Exécuter les contrôles Ruff/docs et le gate d’image CI, y compris son scan.

## Limites à conserver

La cible de cette recette est Linux x86_64/CPython 3.13/musl. Un autre tag de
plateforme doit être construit et qualifié explicitement. Le fonctionnement du
codec n’est pas un test réseau/REST, une migration ni une mesure de performance.
Le mode Python nécessite encore des mesures représentatives avant la livraison.

## Résultat sur l’image applicative

[CI43021809335](https://github.com/HyMathew/cinevault-api/actions/runs/43021809335),
commit `e65ef32ff89066edcb8a2d7f2bdc4d29bcc25a48`, réussie le11novembre2026.
Image construite : `sha256:16923e8d2155c48f8f0c58dd17c62d8ab0f94f529876ad645786f01e20db9098`.
Le reçu confirme UID10001, mode Python, Int64 exact,10schémas/22index,
notices présentes, outils de build absents, healthcheck et nettoyage réussis.
Le scan Grype0.121.2 ne relève aucun High/Critical au moment de l’exécution ;
il conserve **5 correspondances Medium et1 Low**, dont plusieurs pour le même
avis Busybox. Ce gate ne signifie pas absence de vulnérabilité ; qualification
et suivi des avis résiduels restent nécessaires. L’image n’a pas été publiée
ni déployée par ce job de branche.
