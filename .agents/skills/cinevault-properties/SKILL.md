---
name: cinevault-properties
description: Use when reviewing or designing CineVault tests for serialization, HTML form roundtrips, atomic imports, lending or reservation invariants, FIFO, idempotency, or ETag concurrency. Not for broad fuzzing, benchmarking, or a security certification.
license: CC-BY-SA-4.0
---

# Propriétés métier CineVault

Texte adapté du property-based-testing de Trail of Bits, sous CC-BY-SA-4.0. [Attribution, source et modifications](ATTRIBUTION.md), [licence du texte](LICENSE). Cette licence documentaire est distincte de la licence MIT du logiciel CineVault ; aucun collecteur, outil ou dépendance n'est fourni ici.

Choisir une propriété qui contraint un contrat observable : inverse, oracle indépendant ou invariant de transition. Lire le contrat, les tests et le chemin de données avant d'ajouter un générateur. « Ne plante pas » ou une assertion qui recopie l'algorithme n'établit pas la propriété utile. Si le code n'a pas de propriété pertinente, garder un test d'exemple ; ne pas refactorer uniquement pour justifier une nouvelle bibliothèque.

| Domaine | Propriété utile et frontière à exercer |
|---|---|
| JSON/CSV | Export → import dans un magasin isolé préserve les objets canoniques, identifiants, références, accents, guillemets et multiligne, selon transformations documentées. Ne pas exiger l'identité octet à octet du CSV protégé pour tableur. |
| Formulaire HTML | GET du formulaire d'édition → extraction réelle des champs rendus → soumission inchangée → relecture API préserve listes d'acteurs/genres déjà valides. Exercices : virgule dans un nom, retours de ligne, caractères spéciaux et plusieurs valeurs ; tenir compte des normalisations explicitement prévues. Un simple encode/décode Python contourne cette frontière. |
| Import atomique | Un élément invalide, même tardif, ne modifie ni œuvres ni exemplaires existants et ne crée pas de lot partiel. Un événement d'audit de refus peut être ajouté : ne pas exiger que tout le fichier de stockage reste identique. |
| Prêts/réservations | Au plus un prêt actif par exemplaire ; références valides ; priorité FIFO parmi réservations éligibles selon séquence métier, pas ordre aléatoire des dictionnaires. Annulation/retour libère ou réattribue selon contrat, sans fuite de données privées. |
| Versions/idempotence | Une version obsolète donne 412 sans écraser l'objet ; version absente suit le refus prévu. Même clé et même import ne crée pas de second lot ; clé réutilisée avec autre contenu ne devient pas une opération nouvelle. |

Construire des entrées valides directement ; séparer cas invalides, limites et séquences d'actions. Éviter les filtres qui rejettent presque tout. Figer horloge et état initial lorsque l'ordre ou l'échéance intervient. Le modèle de référence reste plus simple et indépendant de l'implémentation.

Lors d'un échec, conserver exemple minimal, graine si générateur, état initial, commandes et résultat exact. Vérifier si le contrat ou la propriété est erroné avant de changer le code. Ne pas élargir les préconditions pour effacer un contre-exemple valide.

Utiliser l'outillage déjà présent ; déterminer depuis le projet les commandes applicables. Cette entrée n'installe ni Hypothesis ni mutmut et ne revendique aucune campagne générative. Distinguer lecture de tests existants, cas déterministes exécutés, génération et mutation réellement exécutées ; quelques contre-exemples ne prouvent ni couverture exhaustive ni score global.
