// Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
// Licensed under the Apache License, Version 2.0 (see LICENSE).

use std::collections::HashSet;
use std::fs;
use std::mem;
use std::path::Path;

use toml::value::Table;
use toml::Value;

use super::id::{NameTransform, OptionId};
use super::parse::parse_string_list;
use super::{ListEdit, ListEditAction, OptionsSource, StringDict};

#[derive(Clone)]
pub struct Config {
  config: Value,
}

impl Config {
  pub fn default() -> Config {
    Config {
      config: Value::Table(Table::new()),
    }
  }

  pub fn parse<P: AsRef<Path>>(file: P) -> Result<Config, String> {
    let config_contents = fs::read_to_string(&file).map_err(|e| {
      format!(
        "Failed to read config file {}: {}",
        file.as_ref().display(),
        e
      )
    })?;
    let config = config_contents.parse::<Value>().map_err(|e| {
      format!(
        "Failed to parse config file {}: {}",
        file.as_ref().display(),
        e
      )
    })?;
    if !config.is_table() {
      return Err(format!(
        "Expected the config file {} to contain a table but contained a {}: {}",
        file.as_ref().display(),
        config.type_str(),
        config
      ));
    }
    if let Some((key, section)) = config
      .as_table()
      .unwrap()
      .iter()
      .find(|(_, section)| !section.is_table())
    {
      return Err(format!(
        "Expected the config file {} to contain tables per section, but section {} contained a {}: {}",
        file.as_ref().display(),
        key,
        section.type_str(),
        section
      ));
    }

    Ok(Config { config })
  }

  pub fn merged<I: IntoIterator<Item = Config>>(config: I) -> Config {
    config
      .into_iter()
      .fold(Config::default(), |acc, config| acc.merge(config))
  }

  fn option_name(id: &OptionId) -> String {
    id.name("_", NameTransform::None)
  }

  fn extract_string_list(option_name: &str, value: &Value) -> Result<Vec<String>, String> {
    if let Some(array) = value.as_array() {
      let mut items = vec![];
      for item in array {
        if let Some(value) = item.as_str() {
          items.push(value.to_owned())
        } else {
          return Err(format!(
            "Expected {option_name} to be an array of strings but given {value} containing non string item {item}"
          ));
        }
      }
      Ok(items)
    } else {
      Err(format!(
        "Expected {option_name} to be a toml array or Python sequence, but given {value}."
      ))
    }
  }

  fn get_value(&self, id: &OptionId) -> Option<&Value> {
    self
      .config
      .get(id.scope())
      .and_then(|table| table.get(Self::option_name(id)))
  }

  pub(crate) fn merge(mut self, mut other: Config) -> Config {
    let mut map = mem::take(self.config.as_table_mut().unwrap());
    let mut other = mem::take(other.config.as_table_mut().unwrap());
    // Merge overlapping sections.
    for (scope, table) in &mut map {
      if let Some(mut other_table) = other.remove(scope) {
        table
          .as_table_mut()
          .unwrap()
          .extend(mem::take(other_table.as_table_mut().unwrap()));
      }
    }
    // And then extend non-overlapping sections.
    map.extend(other);
    Config {
      config: Value::Table(map),
    }
  }
}

impl OptionsSource for Config {
  fn display(&self, id: &OptionId) -> String {
    format!("{id}")
  }

  fn get_string(&self, id: &OptionId) -> Result<Option<String>, String> {
    if let Some(value) = self.get_value(id) {
      if let Some(string) = value.as_str() {
        Ok(Some(string.to_owned()))
      } else {
        Err(format!("Expected {id} to be a string but given {value}."))
      }
    } else {
      Ok(None)
    }
  }

  fn get_bool(&self, id: &OptionId) -> Result<Option<bool>, String> {
    if let Some(value) = self.get_value(id) {
      if let Some(bool) = value.as_bool() {
        Ok(Some(bool))
      } else {
        Err(format!("Expected {id} to be a bool but given {value}."))
      }
    } else {
      Ok(None)
    }
  }

  fn get_int(&self, id: &OptionId) -> Result<Option<i64>, String> {
    if let Some(value) = self.get_value(id) {
      if let Some(float) = value.as_integer() {
        Ok(Some(float))
      } else {
        Err(format!("Expected {} to be an int but given {}.", id, value))
      }
    } else {
      Ok(None)
    }
  }

  fn get_float(&self, id: &OptionId) -> Result<Option<f64>, String> {
    if let Some(value) = self.get_value(id) {
      if let Some(float) = value.as_float() {
        Ok(Some(float))
      } else {
        Err(format!("Expected {id} to be a float but given {value}."))
      }
    } else {
      Ok(None)
    }
  }

  fn get_string_list(&self, id: &OptionId) -> Result<Option<Vec<ListEdit<String>>>, String> {
    if let Some(table) = self.config.get(id.scope()) {
      let option_name = Self::option_name(id);
      let mut list_edits = vec![];
      if let Some(value) = table.get(&option_name) {
        match value {
          Value::Table(sub_table) => {
            if sub_table.is_empty()
              || !sub_table.keys().collect::<HashSet<_>>().is_subset(
                &["add".to_owned(), "remove".to_owned()]
                  .iter()
                  .collect::<HashSet<_>>(),
              )
            {
              return Err(format!(
                "Expected {option_name} to contain an 'add' element, a 'remove' element or both but found: {sub_table:?}"
              ));
            }
            if let Some(add) = sub_table.get("add") {
              list_edits.push(ListEdit {
                action: ListEditAction::Add,
                items: Self::extract_string_list(&format!("{option_name}.add"), add)?,
              })
            }
            if let Some(remove) = sub_table.get("remove") {
              list_edits.push(ListEdit {
                action: ListEditAction::Remove,
                items: Self::extract_string_list(&format!("{option_name}.remove"), remove)?,
              })
            }
          }
          Value::String(v) => {
            list_edits.extend(parse_string_list(v).map_err(|e| e.render(option_name))?);
          }
          value => list_edits.push(ListEdit {
            action: ListEditAction::Replace,
            items: Self::extract_string_list(&option_name, value)?,
          }),
        }
      }
      if !list_edits.is_empty() {
        return Ok(Some(list_edits));
      }
    }
    Ok(None)
  }

  fn get_string_dict(&self, id: &OptionId) -> Result<Option<StringDict>, String> {
    let section = if let Some(table) = self.config.get(&id.scope()) {
      table
    } else {
      return Ok(None);
    };

    // Extract a table, or immediately return a string literal for the caller to parse.
    let option_table = match section.get(&Self::option_name(id)) {
      Some(Value::String(s)) => return Ok(Some(StringDict::Literal(s.clone()))),
      Some(Value::Table(t)) => t,
      None => return Ok(None),
      Some(v) => {
        return Err(format!(
          "Expected {} to be of type string or table, but was a {}: {}",
          self.display(&id),
          v.type_str(),
          v
        ));
      }
    };

    Ok(Some(StringDict::Native(
      option_table.clone().into_iter().collect(),
    )))
  }
}
