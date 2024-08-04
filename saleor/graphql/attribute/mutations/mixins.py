from django.core.exceptions import ValidationError
from django.utils.text import slugify
from text_unidecode import unidecode

from ....attribute import ATTRIBUTE_PROPERTIES_CONFIGURATION, AttributeInputType
from ....attribute import models as models
from ....attribute.error_codes import AttributeErrorCode
from ....core.utils import prepare_unique_slug
from ...core import ResolveInfo
from ...core.validators import validate_slug_and_generate_if_needed


class AttributeMixin:
    # must be redefined by inheriting classes
    ATTRIBUTE_VALUES_FIELD: str
    ONLY_SWATCH_FIELDS = ["file_url", "content_type", "value"]

    @classmethod
    def clean_values(cls, cleaned_input, attribute):
        """Clean attribute values.

        Transforms AttributeValueCreateInput into AttributeValue instances.
        Slugs are created from given names and checked for uniqueness within
        an attribute.
        """
        values_input = cleaned_input.get(cls.ATTRIBUTE_VALUES_FIELD)
        attribute_input_type = cleaned_input.get("input_type") or attribute.input_type

        if values_input is None:
            return

        if (
            values_input
            and attribute_input_type not in AttributeInputType.TYPES_WITH_CHOICES
        ):
            raise ValidationError(
                {
                    cls.ATTRIBUTE_VALUES_FIELD: ValidationError(
                        "Values cannot be used with "
                        f"input type {attribute_input_type}.",
                        code=AttributeErrorCode.INVALID.value,
                    )
                }
            )

        is_swatch_attr = attribute_input_type == AttributeInputType.SWATCH

        slug_list = list(attribute.values.values_list("slug", flat=True))

        for value_data in values_input:
            cls._validate_value(attribute, value_data, is_swatch_attr, slug_list)

    @classmethod
    def generate_slug(cls, ref: str = None, value: str = None, name: str = None,
                      code: str = None, slug_list: list = None) -> str:
        """ Generate a slug from ref, value, name, and code ensuring it conforms to slug standards """
        slug_base = ""

        if ref and value:
            slug_base = f"{ref}-{value}"
        elif code and name:
            slug_base = f"{name}-{code}"
        elif name:
            slug_base = name
        else:
            slug_base = ref or value or code or "default-slug"

        slug = slugify(unidecode(slug_base))

        if slug == "":
            slug = "default-slug"  # Fallback to a default value if slug is empty

        if slug_list is None:
            slug_list = []

        return prepare_unique_slug(slug, slug_list)




    @classmethod
    def _validate_value(
        cls,
        attribute: models.Attribute,
        value_data: dict,
        is_swatch_attr: bool,
        slug_list: list,
    ):
        """Validate the new attribute value."""
        additional_fields = value_data.get("additional_fields", {})
        ref = additional_fields.get("ref")
        value = additional_fields.get("value")
        code = additional_fields.get("code")
        name = value_data.get("name")

        if not additional_fields:
            raise ValidationError(
                {
                    cls.ATTRIBUTE_VALUES_FIELD: ValidationError(
                        "The additional_fields field is required.",
                        code=AttributeErrorCode.REQUIRED.value,
                    )
                }
            )

        # Ensure that either (ref and value) or (name and code) are provided
        if (ref is None or value is None) and (name is None or code is None):
            raise ValidationError(
                {
                    cls.ATTRIBUTE_VALUES_FIELD: ValidationError(
                        "Either both (ref and value) or both (name and code) are required in additional_fields.",
                        code=AttributeErrorCode.REQUIRED.value,
                    )
                }
            )

        # Generate a unique slug based on available fields
        slug_value = cls.generate_slug(ref=ref, value=value, name=name, code=code,
                                       slug_list=slug_list)
        value_data["slug"] = slug_value
        slug_list.append(slug_value)

        # Validate the attribute value based on its type
        if is_swatch_attr:
            cls.validate_swatch_attr_value(value_data)
        else:
            cls.validate_non_swatch_attr_value(value_data)

        # Create and validate the attribute value instance
        attribute_value = models.AttributeValue(**value_data, attribute=attribute)
        try:
            attribute_value.full_clean()
        except ValidationError as validation_errors:
            for field, err in validation_errors.error_dict.items():
                if field == "attribute":
                    continue
                errors = []
                for error in err:
                    error.code = AttributeErrorCode.INVALID.value
                    errors.append(error)
                raise ValidationError({cls.ATTRIBUTE_VALUES_FIELD: errors})

    @classmethod
    def validate_non_swatch_attr_value(cls, value_data: dict):
        if any([value_data.get(field) for field in cls.ONLY_SWATCH_FIELDS]):
            raise ValidationError(
                {
                    cls.ATTRIBUTE_VALUES_FIELD: ValidationError(
                        "Cannot define value, file and contentType fields "
                        "for not swatch attribute.",
                        code=AttributeErrorCode.INVALID.value,
                    )
                }
            )

    @classmethod
    def validate_swatch_attr_value(cls, value_data: dict):
        if value_data.get("value") and value_data.get("file_url"):
            raise ValidationError(
                {
                    cls.ATTRIBUTE_VALUES_FIELD: ValidationError(
                        "Cannot specify both value and file for swatch attribute.",
                        code=AttributeErrorCode.INVALID.value,
                    )
                }
            )

    @classmethod
    def clean_attribute(cls, instance, cleaned_input):
        try:
            cleaned_input = validate_slug_and_generate_if_needed(
                instance, "name", cleaned_input
            )
        except ValidationError as error:
            error.code = AttributeErrorCode.REQUIRED.value
            raise ValidationError({"slug": error})
        cls._clean_attribute_settings(instance, cleaned_input)

        return cleaned_input

    @classmethod
    def _clean_attribute_settings(cls, instance, cleaned_input):
        """Validate attributes settings.

        Ensure that any invalid operations will be not performed.
        """
        attribute_input_type = cleaned_input.get("input_type") or instance.input_type
        errors = {}
        for field in ATTRIBUTE_PROPERTIES_CONFIGURATION.keys():
            allowed_input_type = ATTRIBUTE_PROPERTIES_CONFIGURATION[field]
            if attribute_input_type not in allowed_input_type and cleaned_input.get(
                field
            ):
                errors[field] = ValidationError(
                    f"Cannot set {field} on a {attribute_input_type} attribute.",
                    code=AttributeErrorCode.INVALID.value,
                )
        if errors:
            raise ValidationError(errors)

    @classmethod
    def _save_m2m(cls, info: ResolveInfo, attribute, cleaned_data):
        super()._save_m2m(info, attribute, cleaned_data)  # type: ignore[misc] # mixin
        values = cleaned_data.get(cls.ATTRIBUTE_VALUES_FIELD) or []
        for value in values:
            attribute.values.create(**value)
