# Admission des opérations anonymes

La protection des mots de passe reste Argon2id, temps 3, mémoire 64 Mio, parallélisme 2. Les limites d'admission n'affaiblissent ni ce hachage ni les contrôles bearer, rôle, propriétaire ou CSRF du frontend.

## Source réseau et service de présentation

Le pair TCP de l'API est normalement le service web, commun à ses visiteurs. Il ne représente pas chaque navigateur. En déploiement Enterprise avec frontend, configurer une **clé dédiée partagée uniquement par les deux services**, distincte des clés de session web et de compteurs API, via `CLIENT_CONTEXT_KEY_FILE`. Minimum 32 octets ; aucun secret par défaut. La factory Python accepte aussi `CLIENT_CONTEXT_KEY` en bytes, prioritaire sur le fichier. Une clé fournie invalide ou illisible empêche le démarrage.

L'API active `CLIENT_CONTEXT_REQUIRED=true` pour exiger les assertions du web sur les POST de connexion et d'inscription. Sans ce mode, les clients API directs sans assertion conservent leur pair observé. Une assertion présente, même en mode direct, doit toujours être complète et valide. Le mode obligatoire sans clé est refusé au démarrage. Les autres routes et OPTIONS automatiques conservent leur fonctionnement.

Le vrai client HTTP web garde les signatures `login(data)` et `register(data)`. Lorsqu'une clé est configurée, il construit les en-têtes depuis son propre `request.remote_addr`, jamais depuis X-Forwarded-For, puis signe les octets JSON UTF-8 qu'il transmet. Aucune redirection ni répétition automatique. Un appel signé hors contexte de requête web est refusé 503.

Les trois en-têtes sont `X-CineVault-Client-IP`, `X-CineVault-Client-Time` et `X-CineVault-Client-Signature`. L'adresse doit être IPv4/IPv6 canonique sans identifiant de zone ; le temps est une seconde Unix décimale canonique, avec écart absolu au plus 30 secondes. La signature est HMAC-SHA256 hexadécimal minuscule de cette suite ASCII séparée par LF :

```text
cinevault-client-v1
<temps Unix>
<adresse canonique>
POST
<chemin API>
<SHA256 des octets exacts du corps>
```

En-tête partiel, signature fausse, corps/adresse/chemin modifié ou date hors fenêtre : 401 sans calcul Argon2. Cette assertion identifie une source de limitation, **elle n'authentifie pas un utilisateur** et ne remplace jamais son mot de passe/bearer. Elle n'assure pas la confidentialité du transport et n'est pas une garantie d'unicité contre le rejeu pendant 30 secondes ; un rejeu ne permet pas de changer la source ou le corps signé. Les horloges des services doivent être synchronisées. Les visiteurs derrière une véritable adresse NAT commune partagent encore la limite de cette adresse.

Le login conserve cinq échecs par compte normalisé ou source vérifiée pendant 900 secondes, sous le verrou métier existant. Les clés persistées sont des HMAC, sans adresse ni nom en clair. Une connexion réussie ne réinitialise pas les compteurs. Les sessions déjà ouvertes ne consomment pas ce budget.

## Inscription avant travail coûteux

Ordre : validation des champs, lecture du doublon/capacité, admission partagée, hachage hors verrou métier, puis insertion sous transaction avec **nouveau** contrôle d'unicité/capacité. Un doublon reçoit 409 et un magasin de 10 000 comptes 503 avant hachage. La vérification finale reste nécessaire contre les courses avec d'autres écrivains.

L'état technique dérivé est `<ENTERPRISE_DATA_FILE>.registration.json`, verrou stable au même nom suivi de `.lock`. Ce fichier contient version 1, début de fenêtre Unix, compteur global et dictionnaire de sources HMAC. Aucune donnée de compte ni secret ; il accompagne le document métier sur son volume. Ce n'est pas une seconde base métier. Lecture bornée à 64 Kio, formes et compteurs stricts, somme des sources cohérente ; un état invalide ou une horloge inversée provoque 503 en préservant le fichier, sans remise à zéro silencieuse.

Fenêtre 60 secondes : 20 calculs au plus par source et 60 au total. La réservation est écrite atomiquement avant le calcul, donc une panne de worker ne rembourse pas le budget. Un verrou interprocessus non bloquant permet **un calcul d'inscription à la fois** ; un concurrent reçoit 429 plutôt que d'occuper un autre worker en attente. Aucun verrou métier n'est gardé pendant le hachage d'inscription. Les comptes/sessions, le catalogue et les prêts utilisent toujours leur verrou métier distinct.

Budget épuisé ou calcul concurrent : 429 avec `Retry-After` entier entre 1 et 60 secondes. Le frontend conserve le statut et le délai borné pour le navigateur. Défaillance d'accès/validation de l'admission : 503 ; aucun compte partiel. Le cache de métadonnées ne peut utiliser le fichier d'admission ou son verrou.

Ce contrôle borne le coût admis ; il ne constitue pas une protection universelle contre un déni de service réseau. L'admission globale peut elle-même refuser temporairement un visiteur légitime en surcharge. Les limites amont et la confidentialité du transport relèvent du déploiement. Bootstrap et reprise locale gardent leur autorité opérateur et n'utilisent pas l'admission HTTP.
