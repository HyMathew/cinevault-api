SUPPORTS = {'numerique', 'dvd', 'blu-ray', 'vhs', 'autre'}
FIELDS = {'titre', 'annee', 'realisateur', 'acteurs', 'support', 'synopsis'}

class ValidationError(Exception):
    def __init__(self, fields):
        self.fields = fields

def is_unicode(value):
    try:
        value.encode('utf-8')
        return True
    except UnicodeEncodeError:
        return False

def validate(data):
    if not isinstance(data, dict):
        raise ValidationError({'body': 'Un objet JSON est requis.'})
    errors = {key: 'Champ inconnu.' for key in data.keys() - FIELDS}
    result = {}
    for key, limit in [('titre', 200), ('realisateur', 120)]:
        value = data.get(key)
        if not isinstance(value, str) or not is_unicode(value) or not 1 <= len(value.strip()) <= limit:
            errors[key] = f'Chaîne requise de 1 à {limit} caractères.'
        else:
            result[key] = value.strip()
    year = data.get('annee')
    if type(year) is not int or not 1888 <= year <= 2100:
        errors['annee'] = 'Année entière comprise entre 1888 et 2100.'
    else:
        result['annee'] = year
    actors = data.get('acteurs')
    if not isinstance(actors, list) or len(actors) > 30 or any(not isinstance(a, str) or not is_unicode(a) or not 1 <= len(a.strip()) <= 120 for a in actors):
        errors['acteurs'] = 'Liste de 0 à 30 noms non vides (120 caractères maximum).'
    else:
        result['acteurs'] = [a.strip() for a in actors]
    support = data.get('support')
    if not isinstance(support, str) or support not in SUPPORTS:
        errors['support'] = 'Support invalide.'
    else:
        result['support'] = support
    synopsis = data.get('synopsis', '')
    if not isinstance(synopsis, str) or not is_unicode(synopsis) or len(synopsis) > 4000:
        errors['synopsis'] = 'Chaîne de 4000 caractères maximum.'
    else:
        result['synopsis'] = synopsis
    if errors:
        raise ValidationError(errors)
    return result
