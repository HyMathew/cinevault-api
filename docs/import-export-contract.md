# Import/export du catalogue

Le transfert est ouvert aux gestionnaires et administrateurs en mode Enterprise. Il porte exclusivement sur les métadonnées des films et leurs exemplaires partagés. Les comptes, sessions, collections privées, prêts, réservations, audits et reçus restent hors du contenu transféré. Les sauvegardes d'exploitation du document complet restent confidentielles et distinctes.

## Contrat HTTP

- `POST /api/v1/catalogue/import` : corps JSON `{format:"json"|"csv",content:objet|texte,duplicate_policy:"reject"|"skip"|"append_copies"}`. `content` doit être un texte pour CSV ; JSON accepte un objet ou un texte JSON strict. `Idempotency-Key` obligatoire, syntaxe 16–128 caractères ASCII alphanumériques, tiret/soulignement.
- `GET /api/v1/catalogue/export?format=json|csv` : réponse toujours JSON `{format,content,filename}`. Même un CSV circule dans l'enveloppe JSON entre services. Le frontend en fait un téléchargement au type approprié et nom fixe.
- Succès import : 200 `{operation_id,created_movies,created_copies,skipped_movies,skipped_copies}`. Succès export : 200. Toutes les réponses portent `Cache-Control: no-store`.
- Erreurs : 401 identité absente/invalide, 403 rôle, 400 syntaxe JSON externe, 413 corps HTTP trop gros, 415 type HTTP, 422 format/contenu/limites, 428 clé manquante, 409 conflit de lot ou de clé, 503 stockage/capacité de reçus. Aucune donnée brute du fichier dans l'erreur.

## JSON versionné

`{format_version:1,movies:[...]}` ; chaque film contient `title,year,director,actors,synopsis,copies`, et éventuellement `genres,id,external_source`. L'annotation externe est `{qid,revision,retrieved_at}` ou null ; les valeurs sont validées sans I/O externe. Chaque copie contient `support,location,condition` et éventuellement `id`. Champs inconnus interdits, y compris les tableaux privés. UTF-8 valide, entiers réels (booléens refusés), limites de champs du catalogue, clés JSON dupliquées et NaN rejetés. `genres` omis devient `[]`.

Le schéma `schemas/catalogue-transfer-v1.schema.json` et son exemple décrivent ce format, distinct du document interne v2. Les identifiants fournis doivent être des UUID canoniques uniques dans le fichier et sans collision globale. Les identifiants exportés peuvent ainsi être préservés dans un nouveau catalogue. Les références et identifiants des autres entités ne sont jamais importés.

## CSV

UTF-8 avec ou sans un BOM initial, séparateur **virgule**, guillemet double standard. En-tête exact et dans cet ordre :

```text
movie_id,copy_id,title,year,director,actors,genres,synopsis,support,location,condition
```

Une variante ajoute la colonne finale `external_source`, objet JSON compact ou null. L'export choisit cette variante si un film porte l'annotation ; l'en-tête historique reste accepté. Une cellule vide signifie champ absent, `null` signifie retrait explicite. Les anciens lecteurs stricts doivent être mis à jour pour lire les exports enrichis.

Une ligne par exemplaire. Les quatre champs `copy_id,support,location,condition` tous vides représentent un film sans exemplaire. Dès qu'un de ces champs est rempli, tous les attributs de copie sont validés. `movie_id` et `copy_id` sont facultatifs à l'import. `actors` et `genres` sont des tableaux JSON de chaînes dans la cellule, donc cités/échappés selon CSV. Les retours à la ligne et les virgules dans les champs sont pris en charge. Les colonnes supplémentaires, absentes, CSV malformé et métadonnées contradictoires pour un film groupé sont rejetés.

Les lignes sont regroupées par `movie_id` s'il est renseigné, sinon par titre/année/réalisateur normalisés. JSON est le format sans perte. Le CSV exporté cite toutes les cellules et préfixe par une apostrophe les valeurs commençant par tabulation/CR/LF ou par `=,+,-,@` après espaces initiaux. Cette transformation est conservée lors d'un réimport CSV ; elle n'est pas retirée automatiquement. L'export JSON conserve le texte exact. Aucun contenu importé n'est évalué ou exécuté.

## Doublons et atomicité

Le fichier est intégralement normalisé avant préparation. Les doublons internes de film et d'identifiants sont invalides. Clé naturelle d'un film : titre et réalisateur normalisés, année.

- `reject` (défaut) : un film déjà présent ou une collision annule tous les ajouts du lot.
- `skip` : un film déjà présent et tous ses exemplaires entrants sont ignorés ; compteurs explicites. Aucun remplacement.
- `append_copies` : métadonnées existantes strictement identiques exigées ; seuls les nouveaux exemplaires sont ajoutés. Une copie entrante d'identifiant déjà présent est ignorée seulement si son film et ses attributs sont identiques. Un identifiant absent crée un nouvel exemplaire, même si ses attributs ressemblent à une copie existante.

Un film est associé à l'unique film existant qui porte sa clé naturelle. Un identifiant entrant différent et libre ne remplace pas l'identifiant existant. Une clé naturelle portée par plusieurs films historiques provoque un conflit 409 pour toutes les politiques, y compris si le fichier contient un UUID. Le lot est refusé sans choisir un film ni modifier les doublons. Un UUID déjà utilisé par une autre entité provoque également un conflit pour toute création envisagée.

Une copie complète du document est préparée sous verrou, puis seuls les tableaux films/exemplaires remplacent les anciens après réussite de tout le lot. Erreur de validation/conflit : aucun ajout métier partiel. Le journal d'échec et l'activité de session peuvent néanmoins être persistés. Les droits ne proviennent jamais du formulaire.

## Limites et répétition

Fichier décodé de **1 Mio maximum**, 1 000 films et 5 000 exemplaires par lot. Côté API, l'enveloppe JSON HTTP dispose de 8 Mio pour tenir compte de l'expansion des échappements ; côté web, l'enveloppe multipart est bornée à 2 Mio et le fichier est lu sur 1 Mio + 1 octet. Les routes ordinaires gardent 64 Kio. Un catalogue exporté dépassant ces limites doit être séparé en lots avant réimport ; aucun découpage automatique n'est encore fourni.

Clé d'opération liée à l'acteur, au type d'opération et à l'empreinte du corps canonique avec politique par défaut explicitée. Même contenu/même clé : résultat initial, sans nouvel ajout ni nouvel événement de succès, avec `Idempotency-Replayed: true`. Autre contenu ou autre type d'opération avec la même clé : 409. Garantie bornée à **24 h**, capacité 10 000 reçus. Répéter après expiration est une nouvelle opération ; `append_copies` peut alors créer d'autres copies sans identifiant. Aucun retry automatique. Le reçu conserve seulement identifiant d'opération et compteurs, jamais le fichier brut ni un secret.

L'audit conserve l'acteur, la date, l'identifiant d'opération et l'action `import.completed`, `import.failed` ou `export.completed`. La rétention couvre les 2 000 derniers événements. Un rejet sémantique ou un conflit de lot produit un événement d'échec. Un rejet HTTP de syntaxe, taille ou authentification et un conflit de clé ne créent pas d'événement d'import.

## Service partagé

Le contrôle initial d'autorité autorise la lecture du corps et sa préparation. Les identifiants et la date d'opération restent stables si la transaction doit être reprise. La transaction finale relit la session et le rôle, planifie les insertions avec les clés naturelles et les UUID concernés, puis valide ensemble le catalogue, la mise à jour de session, l'audit et le reçu. Les lots MongoDB utilisent des écritures groupées et une validation finale des films, exemplaires et références concernés. La recherche de clés naturelles retourne au plus deux résultats par clé pour détecter l'ambiguïté. [Opérateur MongoDB utilisé](https://www.mongodb.com/docs/manual/reference/operator/aggregation/firstN/).

L'export lit films et exemplaires dans le même instantané que le contrôle d'autorité et l'audit. Les deux formats trient les films par UUID et les exemplaires par UUID au sein d'un film. Le catalogue est exporté en entier et peut dépasser les limites d'un seul import. La qualification locale vérifie les requêtes et les lots MongoDB avec un double du pilote. Les temps de transfert et l'exécution du service sur MongoDB restent à mesurer. Les routes HTTP utilisent actuellement JSON.

## Validation

`tests/test_transfer.py` vérifie permissions, rejeu, lot invalide sans ajout partiel, conflits tardifs, collisions avec comptes, politiques, JSON/CSV, accents/virgules/guillemets/retours à la ligne, formules CSV, limites, schémas et refus de secrets dans les reçus. Deux processus indépendants avec la même clé produisent un film, une copie, un reçu et un événement de succès.

`tests/test_transfer_services.py` couvre en plus les clés naturelles ambiguës, l'ordre canonique, le rejeu des anciens reçus, la révocation pendant la préparation et la stabilité après annulation. `tests/test_mongo_transfer_ports.py` vérifie le groupement et les références sur le double du pilote.

La preuve Compose locale distincte vérifie le parcours HTTP multipart → web → API JSON → document et le redémarrage. Les tests web isolés utilisent une API simulée. Le contrôle visuel navigateur des nouveaux écrans et le déploiement distribué restent des validations séparées.
